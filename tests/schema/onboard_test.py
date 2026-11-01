import asyncio
import json
import os
import subprocess

import grpc
import harness
import pytest
from google.protobuf.json_format import MessageToJson

from proto import service_pb2, service_pb2_grpc


def _fetch(pg, db_name, sql, *args):
    async def run():
        conn = await harness.connect(pg, db_name)
        try:
            return [tuple(row) for row in await conn.fetch(sql, *args)]
        finally:
            await conn.close()

    return asyncio.run(run())


def _mpac(pg, db_name, *args, env=None):
    return subprocess.Popen(
        [harness.runfile("MPAC_CLI_BIN"), *args, *harness.db_flags(pg, db_name)],
        env=os.environ | {"MPAC_DB_PASSWORD": pg.password} | (env or {}),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _run(pg, db_name, *args, env=None):
    process = _mpac(pg, db_name, *args, env=env)
    output, _ = process.communicate(timeout=120)
    assert process.returncode == 0, output
    return output


def _onboard(pg, db_name, user_id=harness.ADMIN_ID):
    env = harness.onboarding_env() | {"MPAC_ADMIN_ONBOARDING_ID": user_id}
    return _run(pg, db_name, "onboard", env=env)


def _stored_user(pg, db_name, user_id):
    rows = _fetch(
        pg, db_name, "SELECT proto_jsonb, proto_bytes FROM users WHERE id = $1", user_id
    )
    if not rows:
        return None, None
    user = service_pb2.User()
    user.ParseFromString(rows[0][1])
    return json.loads(rows[0][0]), user


def _canonical_json(message):
    return json.loads(
        MessageToJson(
            message,
            always_print_fields_with_no_presence=True,
            preserving_proto_field_name=True,
        )
    )


def _login(address, user_id, password):
    with grpc.insecure_channel(address) as channel:
        return service_pb2_grpc.MPACStub(channel).LoginUser(
            service_pb2.LoginRequest(user_id=user_id, password=password)
        )


@pytest.fixture(scope="module")
def pg():
    with harness.postgres() as pg:
        yield pg


@pytest.fixture
def migrated_db(pg):
    with harness.database(pg) as db_name:
        _run(pg, db_name, "migrate")
        yield db_name


def test_onboarded_admin_can_log_in(pg, migrated_db):
    _onboard(pg, migrated_db)

    stored_json, stored = _stored_user(pg, migrated_db, harness.ADMIN_ID)
    assert stored.role == service_pb2.ADMIN
    assert stored_json == _canonical_json(stored)

    with harness.server(pg, migrated_db, onboard=False) as address:
        assert (
            _login(address, harness.ADMIN_ID, harness.ADMIN_PASSWORD).id
            == harness.ADMIN_ID
        )
        with grpc.insecure_channel(address) as channel:
            user = service_pb2_grpc.MPACStub(channel).GetUser(
                service_pb2.GetRequest(id=harness.ADMIN_ID),
                metadata=(("x-api-key", harness.ADMIN_API_KEY),),
            )
    assert user.role == service_pb2.ADMIN
    assert user.api_key.active


def test_onboard_is_idempotent(pg, migrated_db):
    _onboard(pg, migrated_db)
    before = _fetch(pg, migrated_db, "SELECT proto_bytes FROM users")

    output = _onboard(pg, migrated_db)

    assert "already exist" in output
    assert _fetch(pg, migrated_db, "SELECT proto_bytes FROM users") == before


def test_onboard_promotes_existing_user_when_no_admin_exists(pg, migrated_db):
    existing = service_pb2.User(id="promote-me@example.com", name="Keep Me")
    existing.role = service_pb2.NORMAL
    _fetch(
        pg,
        migrated_db,
        "INSERT INTO users (id, proto_jsonb, proto_bytes) VALUES ($1, $2::jsonb, $3)",
        existing.id,
        json.dumps(_canonical_json(existing)),
        existing.SerializeToString(),
    )

    _onboard(pg, migrated_db, user_id=existing.id)

    _, promoted = _stored_user(pg, migrated_db, existing.id)
    assert promoted.role == service_pb2.ADMIN
    assert promoted.name == "Keep Me"
    with harness.server(pg, migrated_db, onboard=False) as address:
        assert _login(address, existing.id, harness.ADMIN_PASSWORD).name == "Keep Me"


def test_onboard_defers_to_existing_python_admin(pg, migrated_db):
    with harness.server(pg, migrated_db):
        pass

    _onboard(pg, migrated_db, user_id="second-admin@example.com")

    assert _stored_user(pg, migrated_db, "second-admin@example.com") == (None, None)


def test_concurrent_onboarding_creates_one_admin(pg, migrated_db):
    env = harness.onboarding_env()
    processes = [_mpac(pg, migrated_db, "onboard", env=env) for _ in range(4)]
    results = [(p.wait(timeout=120), p.stdout.read()) for p in processes]

    assert [code for code, _ in results] == [0] * 4, results
    assert _fetch(pg, migrated_db, "SELECT id FROM users") == [(harness.ADMIN_ID,)]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-p", "no:cacheprovider"]))
