import os
import time
import urllib.request

import grpc
import harness
import pytest

from proto import service_pb2, service_pb2_grpc

MEMBER_ID = "contract-member@example.com"
OUTSIDER_ID = "contract-outsider@example.com"
DEBUG_MODEL_ID = "debug/debug_random_model_1"
KNOW_IT_ALL_MODEL_ID = "debug/know_it_all"


BLOBS_ENABLED = os.environ.get("MPAC_CONTRACT_BLOBS") == "1"


@pytest.fixture(scope="session")
def blob_store():
    if not BLOBS_ENABLED:
        yield None
        return
    with harness.blob_store() as store:
        yield store


@pytest.fixture(scope="session")
def server_address(blob_store):
    with (
        harness.postgres() as pg,
        harness.database(pg) as db_name,
        harness.server(
            pg, db_name, extra_env=blob_store.env() if blob_store else None
        ) as address,
    ):
        yield address


def attachment_content(attachment):
    if attachment.uri:
        assert not attachment.file
        with urllib.request.urlopen(attachment.uri, timeout=30) as response:
            return response.read()
    return attachment.file


@pytest.fixture(scope="session")
def stub(server_address):
    channel = grpc.intercept_channel(
        grpc.insecure_channel(
            server_address,
            options=[
                ("grpc.max_send_message_length", harness.MAX_MESSAGE_BYTES),
                ("grpc.max_receive_message_length", harness.MAX_MESSAGE_BYTES),
            ],
        ),
        harness.RECORDER,
    )
    yield service_pb2_grpc.MPACStub(channel)
    channel.close()


@pytest.fixture(scope="session")
def admin():
    return (("x-api-key", harness.ADMIN_API_KEY),)


def _create_user(stub, admin, user_id):
    reply = stub.CreateUser(
        service_pb2.User(id=user_id, name=user_id, role=service_pb2.NORMAL),
        metadata=admin,
    )
    assert reply.code == service_pb2.SUCCESS, reply.reason
    return harness.jwt_metadata(user_id)


@pytest.fixture(scope="session")
def member(stub, admin):
    return _create_user(stub, admin, MEMBER_ID)


@pytest.fixture(scope="session")
def outsider(stub, admin):
    return _create_user(stub, admin, OUTSIDER_ID)


@pytest.fixture(scope="session")
def debug_backend(stub, admin):
    reply = stub.CreateBackend(
        service_pb2.Backend(
            name="contract-debug", backend_type=service_pb2.DEBUG_RANDOM
        ),
        metadata=admin,
    )
    assert reply.code == service_pb2.SUCCESS, reply.reason
    return reply.id


@pytest.fixture(scope="session")
def debug_model(stub, admin, debug_backend):
    return stub.GetModel(service_pb2.GetRequest(id=DEBUG_MODEL_ID), metadata=admin)


@pytest.fixture(scope="session")
def know_it_all_model(stub, admin, debug_backend):
    return stub.GetModel(
        service_pb2.GetRequest(id=KNOW_IT_ALL_MODEL_ID), metadata=admin
    )


def create_test(stub, metadata, **fields):
    fields.setdefault("name", f"contract-test-{time.monotonic_ns()}")
    fields.setdefault("type", service_pb2.EVALUATION)
    reply = stub.CreateTest(service_pb2.Test(**fields), metadata=metadata)
    assert reply.code == service_pb2.SUCCESS, reply.reason
    return reply.id


def create_items(stub, metadata, test_id, count):
    ids = []
    for index in range(count):
        reply = stub.CreateTestItem(
            service_pb2.TestItem(
                test_id=test_id,
                question=f"Is {index} even?",
                choices=["yes", "no"],
                answer="yes" if index % 2 == 0 else "no",
                is_relevant=index % 2 == 0,
            ),
            metadata=metadata,
        )
        assert reply.code == service_pb2.SUCCESS, reply.reason
        ids.append(reply.id)
    return ids


def rpc_code(call, *args, **kwargs):
    try:
        result = call(*args, **kwargs)
        if hasattr(result, "__iter__") and not hasattr(result, "ListFields"):
            list(result)
    except grpc.RpcError as error:
        return error.code(), error.details()
    return grpc.StatusCode.OK, ""


def wait_until(predicate, timeout=90, interval=0.5):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met before timeout")
        time.sleep(interval)
