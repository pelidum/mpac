import asyncio
import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import grpc
import harness
import pytest

from proto import service_pb2, service_pb2_grpc

ADMIN = (("x-api-key", harness.ADMIN_API_KEY),)
PROVIDER_KEY = "sk-contract-plaintext-key"


def _tables_containing(pg, db_name, needle):
    async def run():
        conn = await harness.connect(pg, db_name)
        try:
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
            hits = []
            for row in tables:
                table = row["tablename"]
                found = await conn.fetchval(
                    f"""SELECT EXISTS (SELECT 1 FROM "{table}" t WHERE t::text LIKE $1)""",
                    f"%{needle}%",
                )
                hex_needle = needle.encode().hex()
                found = found or await conn.fetchval(
                    f"""SELECT EXISTS (SELECT 1 FROM "{table}" t WHERE t::text LIKE $1)""",
                    f"%{hex_needle}%",
                )
                if found:
                    hits.append(table)
            return hits
        finally:
            await conn.close()

    return asyncio.run(run())


@pytest.fixture(scope="module")
def pg():
    with harness.postgres() as pg:
        yield pg


def _backend(**fields):
    return service_pb2.Backend(
        name="secret-storage",
        backend_type=service_pb2.VLLM,
        base_url="https://provider.example.test/v1",
        **fields,
    )


def test_provider_keys_never_reach_the_database_in_plaintext(pg):
    with (
        harness.database(pg) as db_name,
        harness.server(pg, db_name) as address,
        grpc.insecure_channel(address) as channel,
    ):
        stub = service_pb2_grpc.MPACStub(channel)
        reply = stub.CreateBackend(_backend(api_key=PROVIDER_KEY), metadata=ADMIN)
        assert reply.code == service_pb2.SUCCESS

        assert _tables_containing(pg, db_name, PROVIDER_KEY) == []
        stored = stub.GetBackend(service_pb2.GetRequest(id=reply.id), metadata=ADMIN)
        assert stored.api_key == ""


def test_saving_a_key_without_a_master_key_fails(pg):
    with (
        harness.database(pg) as db_name,
        harness.server(pg, db_name, extra_env={"MPAC_SECRET_KEY_URL": ""}) as address,
        grpc.insecure_channel(address) as channel,
    ):
        stub = service_pb2_grpc.MPACStub(channel)

        with pytest.raises(grpc.RpcError) as error:
            stub.CreateBackend(_backend(api_key=PROVIDER_KEY), metadata=ADMIN)
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        assert (
            stub.CreateBackend(_backend(), metadata=ADMIN).code == service_pb2.SUCCESS
        )
        assert _tables_containing(pg, db_name, PROVIDER_KEY) == []


class _Provider:
    def __enter__(self):
        provider = self
        self.authorizations = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                provider.authorizations.append(self.headers.get("Authorization"))
                body = json.dumps({"data": [{"id": "model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _provider_backend(url, **fields):
    return service_pb2.Backend(
        name="provider",
        backend_type=service_pb2.VLLM,
        base_url=url,
        is_local=True,
        **fields,
    )


def _list_models(stub):
    return list(stub.ListModels(service_pb2.ListRequest(), metadata=ADMIN))


def _sql(pg, db_name, statement, *args):
    async def run():
        conn = await harness.connect(pg, db_name)
        try:
            return await conn.fetch(statement, *args)
        finally:
            await conn.close()

    return asyncio.run(run())


def test_an_api_replica_never_sends_the_old_key_to_a_new_host(pg):
    api_only = {"MPAC_ROLES": "api"}
    with (
        harness.database(pg) as db_name,
        _Provider() as old_host,
        _Provider() as new_host,
        harness.server(pg, db_name, extra_env=api_only) as first_address,
        harness.server(
            pg, db_name, onboard=False, extra_env=api_only
        ) as second_address,
        grpc.insecure_channel(first_address) as first_channel,
        grpc.insecure_channel(second_address) as second_channel,
    ):
        first = service_pb2_grpc.MPACStub(first_channel)
        second = service_pb2_grpc.MPACStub(second_channel)
        backend_id = first.CreateBackend(
            _provider_backend(old_host.url, api_key="sk-old-origin"), metadata=ADMIN
        ).id
        _list_models(second)
        assert "Bearer sk-old-origin" in old_host.authorizations

        update = _provider_backend(new_host.url, id=backend_id, api_key="sk-new-origin")
        update.enabled = True
        assert first.UpdateBackend(update, metadata=ADMIN).code == service_pb2.SUCCESS
        _list_models(second)

        assert new_host.authorizations
        assert set(new_host.authorizations) == {"Bearer sk-new-origin"}


def test_a_rejected_delete_audit_keeps_the_backend_and_its_key(pg):
    with (
        harness.database(pg) as db_name,
        harness.server(pg, db_name) as address,
        grpc.insecure_channel(address) as channel,
    ):
        stub = service_pb2_grpc.MPACStub(channel)
        backend_id = stub.CreateBackend(
            _backend(api_key=PROVIDER_KEY), metadata=ADMIN
        ).id
        _sql(
            pg,
            db_name,
            """CREATE FUNCTION reject_audit() RETURNS trigger AS
               $$ BEGIN RAISE EXCEPTION 'rejected'; END $$ LANGUAGE plpgsql""",
        )
        _sql(
            pg,
            db_name,
            """CREATE TRIGGER reject_audit BEFORE INSERT ON secret_audit
               FOR EACH ROW EXECUTE FUNCTION reject_audit()""",
        )

        with pytest.raises(grpc.RpcError):
            stub.DeleteBackend(service_pb2.DeleteRequest(id=backend_id), metadata=ADMIN)

        assert (
            stub.GetBackend(service_pb2.GetRequest(id=backend_id), metadata=ADMIN).id
            == backend_id
        )
        assert _sql(pg, db_name, "SELECT 1 FROM backend_secrets") != []

        _sql(pg, db_name, "DROP TRIGGER reject_audit ON secret_audit")
        reply = stub.DeleteBackend(
            service_pb2.DeleteRequest(id=backend_id), metadata=ADMIN
        )
        assert reply.code == service_pb2.SUCCESS
        assert _sql(
            pg,
            db_name,
            "SELECT 1 FROM secret_audit WHERE backend_id = $1 AND action = 'delete'",
            backend_id,
        )


@pytest.mark.parametrize(
    "extra_env, expected",
    [
        ({"MPAC_SECRET_KEY_URL": ""}, grpc.StatusCode.FAILED_PRECONDITION),
        (
            {"MPAC_SECRET_KEY_URL": "file:///tmp/mpac-other-master-key"},
            grpc.StatusCode.FAILED_PRECONDITION,
        ),
        (
            {
                "MPAC_SECRET_KEY_URL": "vault+http://127.0.0.1:1/transit/mpac",
                "VAULT_TOKEN": "token",
            },
            grpc.StatusCode.UNAVAILABLE,
        ),
    ],
    ids=["missing_master_key", "wrong_master_key", "key_service_down"],
)
def test_a_key_failure_never_becomes_an_unauthenticated_call(pg, extra_env, expected):
    other_key = extra_env.get("MPAC_SECRET_KEY_URL", "")
    if other_key.startswith("file://"):
        with open(other_key.removeprefix("file://"), "w") as key_file:
            key_file.write(base64.b64encode(os.urandom(32)).decode())
    with harness.database(pg) as db_name, _Provider() as keyed, _Provider() as keyless:
        with (
            harness.server(pg, db_name) as address,
            grpc.insecure_channel(address) as channel,
        ):
            keyed_id = (
                service_pb2_grpc.MPACStub(channel)
                .CreateBackend(
                    _provider_backend(keyed.url, api_key=PROVIDER_KEY), metadata=ADMIN
                )
                .id
            )
        if "vault" in other_key:
            _sql(
                pg,
                db_name,
                """UPDATE backend_secrets SET master_key_id = 'vault:127.0.0.1/transit/mpac',
                   wrapped_key = 'vault:v1:unreachable'""",
            )
        keyed.authorizations.clear()

        with (
            harness.server(pg, db_name, onboard=False, extra_env=extra_env) as address,
            grpc.insecure_channel(address) as channel,
        ):
            stub = service_pb2_grpc.MPACStub(channel)
            with pytest.raises(grpc.RpcError) as listed:
                _list_models(stub)
            assert listed.value.code() == expected
            with pytest.raises(grpc.RpcError) as credits:
                stub.GetCredits(service_pb2.GetRequest(id=keyed_id), metadata=ADMIN)
            assert credits.value.code() == expected
            assert keyed.authorizations == []

            stub.CreateBackend(_provider_backend(keyless.url), metadata=ADMIN)
            assert [model.id for model in _list_models(stub)] != []
            assert keyed.authorizations == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
