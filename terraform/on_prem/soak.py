import argparse
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import grpc
import requests

from proto import service_pb2 as pb
from proto import service_pb2_grpc
from tests.resilience import fake_provider

GIB = 1024**3
PROVIDER_KEY = "sk-soak-provider-key-" + uuid.uuid4().hex


class Filler:
    def __init__(self, size):
        self.left = size
        self.size = size
        self.block = b"mpac soak test data\n" * 52429

    def __len__(self):
        return self.left

    def read(self, count=-1):
        count = min(count if count > 0 else len(self.block), len(self.block), self.left)
        self.left -= count
        return self.block[:count]


class ProviderContainer:
    def __init__(self, network):
        self.name = f"mpac-soak-provider-{uuid.uuid4().hex[:8]}"
        self.network = network
        self.admin_url = ""

    def __enter__(self):
        source = str(Path(fake_provider.__file__).resolve())
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                self.name,
                "--network",
                self.network,
                "-p",
                "127.0.0.1:0:8000",
                "-v",
                f"{source}:/fake_provider.py:ro",
                "python:3.11-slim",
                "python",
                "/fake_provider.py",
            ],
            check=True,
            capture_output=True,
        )
        port = (
            subprocess.run(
                ["docker", "port", self.name, "8000"],
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.split(":")[-1]
            .strip()
        )
        self.admin_url = f"http://127.0.0.1:{port}"
        wait_for(self._ready, 60, "fake provider")
        return self

    def __exit__(self, *exc):
        subprocess.run(
            ["docker", "rm", "-f", self.name], capture_output=True, check=False
        )

    def _ready(self):
        try:
            return requests.get(self.admin_url + "/__state", timeout=2).ok
        except requests.RequestException:
            return False

    @property
    def base_url(self):
        return f"http://{self.name}:8000/v1"

    @property
    def state(self):
        return requests.get(self.admin_url + "/__state", timeout=5).json()

    def configure(self, **values):
        requests.post(
            self.admin_url + "/__config", json=values, timeout=5
        ).raise_for_status()


def wait_for(check, timeout, label):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError(f"{label}: not reached within {timeout} s")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--grpc", required=True)
    parser.add_argument("--ca-cert", required=True)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--server-container", required=True)
    parser.add_argument("--ui-container", required=True)
    parser.add_argument("--network", required=True)
    args = parser.parse_args()
    password = os.environ["MPAC_SMOKE_PASSWORD"]
    ca = str(Path(args.ca_cert).resolve())

    session = requests.Session()
    session.verify = ca
    login = session.post(
        args.url + "/login/password",
        data={"username": args.admin_email, "password": password},
        allow_redirects=False,
        timeout=30,
    )
    assert login.status_code == 303 and session.cookies.get("mpac_jwt"), "login failed"
    print("password login: OK")
    page = session.get(args.url + "/tests/create", timeout=30)
    token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    session.headers["X-CSRF-Token"] = token
    metadata = (("x-jwt-token", session.cookies.get("mpac_jwt")),)

    credentials = grpc.ssl_channel_credentials(Path(ca).read_bytes())
    options = (("grpc.ssl_target_name_override", "localhost"),)
    with (
        grpc.secure_channel(args.grpc, credentials, options) as channel,
        ProviderContainer(args.network) as provider,
    ):
        stub = service_pb2_grpc.MPACStub(channel)
        grpc.channel_ready_future(channel).result(timeout=30)

        backend_id = stub.CreateBackend(
            pb.Backend(
                name="soak-local",
                backend_type=pb.VLLM,
                base_url=provider.base_url,
                is_local=True,
                api_key=PROVIDER_KEY,
            ),
            metadata=metadata,
        ).id
        test_id = stub.CreateTest(
            pb.Test(name="soak", type=pb.EVALUATION), metadata=metadata
        ).id
        for index in range(6):
            stub.CreateTestItem(
                pb.TestItem(
                    test_id=test_id,
                    question=f"Question {index}?",
                    choices=["yes", "no"],
                    answer="yes",
                    is_relevant=True,
                ),
                metadata=metadata,
            )

        def run(models, size):
            replies = list(
                stub.CreateTestRun(
                    pb.TestRunRequest(test_id=test_id, models=models, sample_size=size),
                    metadata=metadata,
                )
            )
            return replies[0].run_id

        def finished(run_id, timeout=240):
            def check():
                try:
                    value = stub.GetTestRun(pb.GetRequest(id=run_id), metadata=metadata)
                except grpc.RpcError as error:
                    if error.code() == grpc.StatusCode.UNAVAILABLE:
                        return None
                    raise
                return value if value.status != pb.IN_PROGRESS else None

            return wait_for(check, timeout, f"run {run_id}")

        def answers(run_id):
            return list(
                stub.ListTestRunAnswers(
                    pb.ListRequest(parent_id=run_id, limit=1000), metadata=metadata
                )
            )

        provider.configure(delay_seconds=1)
        models = [
            pb.Model(id=f"soak-model-{i}", backend_id=backend_id) for i in range(2)
        ]
        run_id = run(models, 6)
        assert finished(run_id).status == pb.SUCCESS
        stored = answers(run_id)
        assert len(stored) == 12 and all(
            a.status == pb.TestRunAnswer.OK for a in stored
        )
        assert provider.state["authorizations"] == [f"Bearer {PROVIDER_KEY}"], (
            "key not decrypted"
        )
        spans = sorted(provider.state["model_spans"].values())
        assert spans[0][1] <= spans[1][0], (
            f"models overlapped on a local backend: {spans}"
        )
        print("multi-model local run, decrypted key, one model at a time: OK")

        audit = list(
            stub.ListSecretAudit(
                pb.ListRequest(parent_id=backend_id), metadata=metadata
            )
        )
        actions = {event.action for event in audit}
        assert {"create", "decrypt"} <= actions, actions
        print(f"secret audit trail: OK ({sorted(actions)})")

        provider.configure(delay_seconds=60)
        run_id = run(models[:1], 2)
        wait_for(lambda: provider.state["in_flight"] >= 2, 30, "requests in flight")
        current = stub.GetTestRun(pb.GetRequest(id=run_id), metadata=metadata)
        current.status = pb.CANCELLED
        assert stub.UpdateTestRun(current, metadata=metadata).code == pb.SUCCESS
        wait_for(
            lambda: provider.state["in_flight"] == 0, 15, "provider connections closed"
        )
        assert finished(run_id).status == pb.CANCELLED
        print("cancellation closes provider connections: OK")

        provider.configure(delay_seconds=3)
        run_id = run(models[:1], 6)
        wait_for(lambda: len(answers(run_id)) >= 1, 60, "first answer")
        subprocess.run(
            ["docker", "kill", args.server_container], check=True, capture_output=True
        )
        subprocess.run(
            ["docker", "start", args.server_container], check=True, capture_output=True
        )
        grpc.channel_ready_future(channel).result(timeout=60)
        wait_for(
            lambda: session.get(args.url + "/login", timeout=10).status_code == 200,
            60,
            "UI",
        )
        assert finished(run_id, timeout=300).status == pb.SUCCESS
        stored = answers(run_id)
        assert len(stored) == 6 and len({a.item_id for a in stored}) == 6
        print("server killed mid-run, run finished with no duplicate answers: OK")

        debug_id = stub.CreateBackend(
            pb.Backend(name="soak-debug", backend_type=pb.DEBUG_RANDOM),
            metadata=metadata,
        ).id
        debug_models = [
            pb.Model(id=f"debug/debug_random_model_{i}", backend_id=debug_id)
            for i in range(1, 6)
        ]
        benchmark_id = next(
            stub.CreateBenchmark(
                pb.BenchmarkRequest(
                    test_id=test_id, models=debug_models, sample_size=6
                ),
                metadata=metadata,
            )
        ).benchmark_id
        seen = []
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            poll = session.get(
                args.url + f"/benchmarks/status/{benchmark_id}/poll", timeout=30
            ).json()
            count = len(poll["match_history"])
            if not seen or seen[-1] != count:
                seen.append(count)
            if poll["status"] != "IN_PROGRESS":
                break
            time.sleep(0.1)
        assert poll["status"] == "SUCCESS" and poll["winner"], poll["status"]
        byes = [m["winner"] for m in poll["match_history"] if len(m["responses"]) == 1]
        assert len(byes) == len(set(byes)) == 2, byes
        assert len(seen) > 3, f"history only grew in steps {seen}"
        print(f"benchmark live match updates {seen}, different BYE each round: OK")

        size = 2 * GIB
        started = time.monotonic()
        upload = session.post(
            args.url + "/attachments/upload",
            params={"name": "soak-2gib.txt", "mime": "text/plain"},
            data=Filler(size),
            timeout=1800,
        )
        assert upload.status_code == 200, upload.text
        attachment_id = upload.json()["id"]
        peak = subprocess.run(
            ["docker", "exec", args.ui_container, "cat", "/sys/fs/cgroup/memory.peak"],
            capture_output=True,
            text=True,
            check=False,
        )
        peak_mib = int(peak.stdout) / 1024**2 if peak.stdout.strip() else -1
        print(
            f"2 GiB browser-path upload through Envoy and the UI: OK "
            f"({time.monotonic() - started:.0f} s, UI peak memory {peak_mib:.0f} MiB)"
        )
        over = session.post(
            args.url + "/attachments/upload",
            params={"name": "over.txt"},
            data=Filler(size + 1),
            timeout=120,
        )
        assert over.status_code == 413, over.status_code
        print("upload over the limit rejected: OK")

        tail = session.get(
            args.url + f"/attachments/{attachment_id}/file",
            headers={"Range": "bytes=2147483640-"},
            timeout=120,
        )
        assert tail.status_code == 206 and len(tail.content) == 8, tail.status_code
        print("streamed range download through the UI: OK")

        big_test = stub.CreateTest(
            pb.Test(name="soak-large", type=pb.EVALUATION), metadata=metadata
        ).id
        stub.CreateTestItem(
            pb.TestItem(
                test_id=big_test,
                question="Large?",
                choices=["yes", "no"],
                answer="yes",
                attachment_id=attachment_id,
            ),
            metadata=metadata,
        )
        replies = list(
            stub.CreateTestRun(
                pb.TestRunRequest(test_id=big_test, models=models[:1], sample_size=1),
                metadata=metadata,
            )
        )
        finished(replies[0].run_id)
        large = answers(replies[0].run_id)
        assert [a.status for a in large] == [pb.TestRunAnswer.BAD_REQUEST], large
        print("attachment over the inference limit stores a visible error: OK")
        print(f"PROVIDER_KEY={PROVIDER_KEY}")
        print(f"BACKEND_ID={backend_id}")
        print(f"ATTACHMENT_ID={attachment_id}")


if __name__ == "__main__":
    main()
