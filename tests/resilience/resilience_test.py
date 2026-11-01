import asyncio
import os
import signal
import subprocess
import time
import urllib.request

import grpc
import harness
import pytest

from proto import service_pb2, service_pb2_grpc
from tests.resilience.fake_provider import FakeProvider

ADMIN = (("x-api-key", harness.ADMIN_API_KEY),)
REFLECTION = "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo"


def _wait_until(check, timeout=120, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout} s")


def _query(pg, db_name, sql, *args):
    async def run():
        conn = await harness.connect(pg, db_name)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def _spawn(pg, db_name, subcommand, port, *flags, extra_env=None):
    env = (
        os.environ
        | {
            "MPAC_DB_PASSWORD": pg.password,
            "MPAC_JWT_SECRET": harness.JWT_SECRET,
            "MPAC_SECRET_KEY_URL": harness.secret_key_url(),
            "PYTHONUNBUFFERED": "1",
        }
        | harness.onboarding_env()
    )
    env |= extra_env or {}
    process = subprocess.Popen(
        [
            harness.runfile("MPAC_CONTRACT_SERVER_BIN"),
            subcommand,
            "--host=127.0.0.1",
            f"--port={port}",
            *flags,
            *harness.db_flags(pg, db_name),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    harness._wait_for_health(f"127.0.0.1:{port}", process)
    return process


def _stop(*processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()


def _stub(port):
    return service_pb2_grpc.MPACStub(grpc.insecure_channel(f"127.0.0.1:{port}"))


def _prepare(stub, provider, items, choices=("yes", "no"), max_concurrency=0):
    backend_id = stub.CreateBackend(
        service_pb2.Backend(
            name="resilience",
            backend_type=service_pb2.VLLM,
            base_url=provider.base_url,
            is_local=True,
            max_concurrency=max_concurrency,
        ),
        metadata=ADMIN,
    ).id
    test_id = stub.CreateTest(
        service_pb2.Test(name="resilience", type=service_pb2.EVALUATION), metadata=ADMIN
    ).id
    for index in range(items):
        stub.CreateTestItem(
            service_pb2.TestItem(
                test_id=test_id,
                question=f"Question {index}?",
                choices=list(choices),
                answer=choices[0],
                is_relevant=True,
            ),
            metadata=ADMIN,
        )
    return test_id, service_pb2.Model(id="resilience-model", backend_id=backend_id)


def _start_run(stub, test_id, model, sample_size):
    replies = list(
        stub.CreateTestRun(
            service_pb2.TestRunRequest(
                test_id=test_id, models=[model], sample_size=sample_size
            ),
            metadata=ADMIN,
        )
    )
    return replies[0].run_id


def _finished(stub, run_id, timeout=180):
    def check():
        run = stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=ADMIN)
        return run if run.status != service_pb2.IN_PROGRESS else None

    return _wait_until(check, timeout=timeout, interval=0.5)


def _answers(stub, run_id):
    return list(
        stub.ListTestRunAnswers(
            service_pb2.ListRequest(parent_id=run_id, limit=1000), metadata=ADMIN
        )
    )


def _answer_attempts(pg, db_name, run_id):
    rows = _query(
        pg,
        db_name,
        "SELECT status, attempts FROM jobs WHERE run_id = $1 AND kind = 'answer_item'",
        run_id,
    )
    return [(row["status"], row["attempts"]) for row in rows]


def _peak_rss_mib(pid):
    with open(f"/proc/{pid}/status") as status:
        for line in status:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    raise AssertionError("no VmHWM")


@pytest.fixture(scope="module")
def pg():
    with harness.postgres() as pg:
        yield pg


@pytest.fixture
def db_name(pg):
    with harness.database(pg) as name:
        yield name


def test_fragmented_streams_decode_across_byte_boundaries(pg, db_name):
    port = harness._free_port()
    server = _spawn(pg, db_name, "serve", port)
    try:
        with FakeProvider() as provider:
            provider.fragment = True
            provider.answer = "oui é"
            stub = _stub(port)
            test_id, model = _prepare(stub, provider, 2, choices=("oui é", "non"))
            run_id = _start_run(stub, test_id, model, 2)

            run = _finished(stub, run_id)

            assert run.status == service_pb2.SUCCESS
            answers = _answers(stub, run_id)
            assert [a.answer for a in answers] == ["oui é", "oui é"]
            assert all(a.status == service_pb2.TestRunAnswer.OK for a in answers)
            assert all(a.ttft > 0 for a in answers)
    finally:
        _stop(server)


def test_a_request_longer_than_the_lease_finishes_once(pg, db_name):
    port = harness._free_port()
    server = _spawn(pg, db_name, "serve", port)
    try:
        with FakeProvider() as provider:
            provider.delay_seconds = 70
            stub = _stub(port)
            test_id, model = _prepare(stub, provider, 1)
            run_id = _start_run(stub, test_id, model, 1)

            run = _finished(stub, run_id)

            assert run.status == service_pb2.SUCCESS
            assert provider.started == 1
            assert _answer_attempts(pg, db_name, run_id) == [("done", 1)]
            assert [a.status for a in _answers(stub, run_id)] == [
                service_pb2.TestRunAnswer.OK
            ]
    finally:
        _stop(server)


def test_killing_a_worker_finishes_the_run_without_duplicate_answers(pg, db_name):
    api_port, first_port, second_port = (harness._free_port() for _ in range(3))
    api = _spawn(pg, db_name, "serve", api_port, "--roles=api")
    first = _spawn(
        pg, db_name, "worker", first_port, "--skip_migrate", "--worker_concurrency=2"
    )
    second = _spawn(
        pg, db_name, "worker", second_port, "--skip_migrate", "--worker_concurrency=2"
    )
    try:
        with FakeProvider() as provider:
            provider.delay_seconds = 3
            stub = _stub(api_port)
            test_id, model = _prepare(stub, provider, 8)
            run_id = _start_run(stub, test_id, model, 8)

            def victim():
                rows = _query(
                    pg, db_name, "SELECT leased_by FROM jobs WHERE status = 'leased'"
                )
                for process in (first, second):
                    if any(f"-{process.pid}-" in row["leased_by"] for row in rows):
                        return process
                return None

            _wait_until(lambda: provider.in_flight == 4)
            target = _wait_until(victim)
            target.send_signal(signal.SIGKILL)
            target.wait(timeout=10)

            run = _finished(stub, run_id)

            assert run.status == service_pb2.SUCCESS
            answers = _answers(stub, run_id)
            assert len(answers) == 8
            assert len({a.item_id for a in answers}) == 8
            assert all(a.status == service_pb2.TestRunAnswer.OK for a in answers)
            attempts = _answer_attempts(pg, db_name, run_id)
            assert {status for status, _ in attempts} == {"done"}
            assert any(count == 2 for _, count in attempts)
            assert provider.started > 8
    finally:
        _stop(api, first, second)


def test_cancelling_a_run_closes_the_active_provider_connection(pg, db_name):
    port = harness._free_port()
    server = _spawn(pg, db_name, "serve", port)
    try:
        with FakeProvider() as provider:
            provider.delay_seconds = 120
            stub = _stub(port)
            test_id, model = _prepare(stub, provider, 2, max_concurrency=2)
            run_id = _start_run(stub, test_id, model, 2)
            _wait_until(lambda: provider.in_flight == 2)

            run = stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=ADMIN)
            run.status = service_pb2.CANCELLED
            assert stub.UpdateTestRun(run, metadata=ADMIN).code == service_pb2.SUCCESS

            _wait_until(lambda: provider.disconnected == 2, timeout=10)
            assert provider.finished == 0
            assert _finished(stub, run_id, timeout=30).status == service_pb2.CANCELLED
    finally:
        _stop(server)


def test_sigterm_hands_active_work_to_the_next_process(pg, db_name):
    port = harness._free_port()
    server = _spawn(pg, db_name, "serve", port)
    restarted = None
    try:
        with FakeProvider() as provider:
            provider.delay_seconds = 300
            stub = _stub(port)
            test_id, model = _prepare(stub, provider, 1)
            run_id = _start_run(stub, test_id, model, 1)
            _wait_until(lambda: provider.in_flight == 1)

            started = time.monotonic()
            server.send_signal(signal.SIGTERM)
            assert server.wait(timeout=35) == 0
            assert time.monotonic() - started < 35
            assert _answer_attempts(pg, db_name, run_id) == [("queued", 0)]

            provider.delay_seconds = 0
            port = harness._free_port()
            restarted = _spawn(pg, db_name, "serve", port)
            run = _finished(_stub(port), run_id)

            assert run.status == service_pb2.SUCCESS
            assert _answer_attempts(pg, db_name, run_id) == [("done", 1)]
    finally:
        _stop(server, *([restarted] if restarted else []))


@pytest.mark.parametrize("enabled", [True, False])
def test_reflection_follows_its_flag(pg, db_name, enabled):
    port = harness._free_port()
    server = _spawn(
        pg,
        db_name,
        "serve",
        port,
        extra_env={"MPAC_ENABLE_REFLECTION": "true" if enabled else "false"},
    )
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            call = channel.stream_stream(REFLECTION)(iter([b"\x3a\x00"]))
            if enabled:
                assert next(call)
            else:
                with pytest.raises(grpc.RpcError) as error:
                    next(call)
                assert error.value.code() == grpc.StatusCode.UNIMPLEMENTED
    finally:
        _stop(server)


def test_large_attachments_stay_within_the_worker_memory_budget(pg, db_name):
    attachment_mib = 40
    jobs = 16
    with harness.blob_store() as blobs:
        port = harness._free_port()
        server = _spawn(pg, db_name, "serve", port, extra_env=blobs.env())
        try:
            with FakeProvider() as provider:
                provider.delay_seconds = 2
                stub = _stub(port)
                test_id, model = _prepare(stub, provider, 0, max_concurrency=jobs)
                for index in range(jobs):
                    content = b"\xff\xd8\xff\xe0" + os.urandom(
                        attachment_mib * 1024 * 1024
                    )
                    upload = stub.CreateAttachmentUpload(
                        service_pb2.CreateAttachmentUploadRequest(
                            name=f"large-{index}.jpg", size_bytes=len(content)
                        ),
                        metadata=ADMIN,
                    )
                    request = urllib.request.Request(
                        upload.upload_uri, data=content, method="PUT"
                    )
                    urllib.request.urlopen(request, timeout=120).close()
                    attachment = stub.FinalizeAttachment(
                        service_pb2.GetRequest(id=upload.attachment_id), metadata=ADMIN
                    )
                    stub.CreateTestItem(
                        service_pb2.TestItem(
                            test_id=test_id,
                            question=f"Question {index}?",
                            choices=["yes", "no"],
                            answer="yes",
                            attachment_id=attachment.id,
                        ),
                        metadata=ADMIN,
                    )
                run_id = _start_run(stub, test_id, model, jobs)

                run = _finished(stub, run_id, timeout=240)

                assert run.status == service_pb2.SUCCESS
                peak = _peak_rss_mib(server.pid)
                print(
                    f"peak worker memory: {peak:.0f} MiB, provider peak {provider.peak_in_flight}"
                )
                assert len(_answers(stub, run_id)) == jobs
                assert peak < 1800, f"peak worker memory {peak:.0f} MiB"
                assert provider.peak_in_flight < jobs
        finally:
            _stop(server)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s", "-p", "no:cacheprovider"]))
