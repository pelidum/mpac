import grpc
import harness
from conftest import DEBUG_MODEL_ID, KNOW_IT_ALL_MODEL_ID, rpc_code

from proto import service_pb2


def test_backend_admin_lifecycle(stub, admin, member):
    reply = stub.CreateBackend(
        service_pb2.Backend(
            id="client-chosen",
            name="contract-vllm",
            backend_type=service_pb2.VLLM,
            base_url="https://vllm.example.com/v1",
            api_key="sk-secret",
            enabled=False,
        ),
        metadata=admin,
    )
    assert reply.code == service_pb2.SUCCESS
    backend_id = reply.id
    assert backend_id != "client-chosen"

    backend = stub.GetBackend(service_pb2.GetRequest(id=backend_id), metadata=admin)
    assert backend.api_key == ""
    assert backend.enabled
    assert backend.created_at_utc.seconds > 0

    backend.enabled = False
    backend.name = "contract-vllm-disabled"
    assert stub.UpdateBackend(backend, metadata=admin).code == service_pb2.SUCCESS
    assert (
        stub.GetBackend(service_pb2.GetRequest(id=backend_id), metadata=admin).name
        == "contract-vllm-disabled"
    )

    admin_view = [
        b.id for b in stub.ListBackends(service_pb2.ListRequest(), metadata=admin)
    ]
    member_view = [
        b.id for b in stub.ListBackends(service_pb2.ListRequest(), metadata=member)
    ]
    assert backend_id in admin_view
    assert backend_id not in member_view
    assert all(
        b.api_key == ""
        for b in stub.ListBackends(service_pb2.ListRequest(), metadata=admin)
    )

    assert (
        stub.DeleteBackend(
            service_pb2.DeleteRequest(id=backend_id), metadata=admin
        ).code
        == service_pb2.SUCCESS
    )
    code, _ = rpc_code(
        stub.GetBackend, service_pb2.GetRequest(id=backend_id), metadata=admin
    )
    assert code == grpc.StatusCode.NOT_FOUND


def test_backend_mutations_require_admin(stub, member, debug_backend):
    backend = service_pb2.Backend(
        id=debug_backend, name="hijack", backend_type=service_pb2.DEBUG_RANDOM
    )
    for call, request in [
        (stub.CreateBackend, backend),
        (stub.UpdateBackend, backend),
        (stub.DeleteBackend, service_pb2.DeleteRequest(id=debug_backend)),
    ]:
        code, _ = rpc_code(call, request, metadata=member)
        assert code == grpc.StatusCode.PERMISSION_DENIED


def test_debug_backend_lists_models(stub, member, debug_backend):
    models = [
        m
        for m in stub.ListModels(service_pb2.ListRequest(), metadata=member)
        if m.backend_id == debug_backend
    ]
    ids = {m.id for m in models}

    assert len(models) == 101
    assert {KNOW_IT_ALL_MODEL_ID, DEBUG_MODEL_ID, "debug/debug_random_model_100"} <= ids


def test_get_model(stub, member, debug_backend):
    model = stub.GetModel(service_pb2.GetRequest(id=DEBUG_MODEL_ID), metadata=member)
    assert model.id == DEBUG_MODEL_ID
    assert model.backend_id == debug_backend

    code, _ = rpc_code(
        stub.GetModel, service_pb2.GetRequest(id="debug/nope"), metadata=member
    )
    assert code == grpc.StatusCode.NOT_FOUND


def test_get_credits_for_debug_backend(stub, member, debug_backend):
    credits = stub.GetCredits(service_pb2.GetRequest(id=debug_backend), metadata=member)
    assert credits.backend_type == "debug_random"
    assert credits.total_credits == 0

    code, _ = rpc_code(
        stub.GetCredits, service_pb2.GetRequest(id="missing"), metadata=member
    )
    assert code != grpc.StatusCode.OK


def test_changing_backend_host_requires_the_key_again(stub, admin):
    backend_id = stub.CreateBackend(
        service_pb2.Backend(
            name="contract-key-host",
            backend_type=service_pb2.VLLM,
            base_url="https://one.example.test/v1",
            api_key="sk-contract",
        ),
        metadata=admin,
    ).id
    try:
        backend = stub.GetBackend(service_pb2.GetRequest(id=backend_id), metadata=admin)
        assert backend.api_key == ""

        backend.description = "same host"
        assert stub.UpdateBackend(backend, metadata=admin).code == service_pb2.SUCCESS

        backend.base_url = "https://two.example.test/v1"
        code, _ = rpc_code(stub.UpdateBackend, backend, metadata=admin)
        assert code == grpc.StatusCode.INVALID_ARGUMENT

        backend.api_key = "sk-new"
        assert stub.UpdateBackend(backend, metadata=admin).code == service_pb2.SUCCESS
    finally:
        stub.DeleteBackend(service_pb2.DeleteRequest(id=backend_id), metadata=admin)


def test_key_changes_are_audited_for_admins_only(stub, admin, member):
    backend_id = stub.CreateBackend(
        service_pb2.Backend(
            name="contract-key-audit",
            backend_type=service_pb2.VLLM,
            base_url="https://audit.example.test/v1",
            api_key="sk-audit",
        ),
        metadata=admin,
    ).id
    backend = stub.GetBackend(service_pb2.GetRequest(id=backend_id), metadata=admin)
    backend.api_key = "sk-audit-2"
    assert stub.UpdateBackend(backend, metadata=admin).code == service_pb2.SUCCESS
    stub.DeleteBackend(service_pb2.DeleteRequest(id=backend_id), metadata=admin)

    events = list(
        stub.ListSecretAudit(
            service_pb2.ListRequest(parent_id=backend_id), metadata=admin
        )
    )
    assert [event.action for event in events] == ["delete", "update", "create"]
    assert events[1].key_version == 2
    assert all(event.actor == harness.ADMIN_ID for event in events)
    assert events[0].created_at_utc.seconds > 0

    code, _ = rpc_code(stub.ListSecretAudit, service_pb2.ListRequest(), metadata=member)
    assert code == grpc.StatusCode.PERMISSION_DENIED
