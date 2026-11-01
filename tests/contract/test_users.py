import grpc
import harness
from conftest import MEMBER_ID, OUTSIDER_ID, rpc_code

from proto import service_pb2


def test_get_user_strips_secrets(stub, admin):
    user = stub.GetUser(service_pb2.GetRequest(id=harness.ADMIN_ID), metadata=admin)
    assert user.password_hash == ""
    assert user.api_key.hash == ""
    assert user.api_key.active


def test_created_normal_user_gets_default_spend_limits(stub, admin, member):
    user = stub.GetUser(service_pb2.GetRequest(id=MEMBER_ID), metadata=admin)
    assert user.role == service_pb2.NORMAL
    assert user.rate_limits.daily_spend == 1000
    assert user.rate_limits.monthly_spend == 10000
    assert user.created_at_utc.seconds > 0


def test_create_user_requires_admin(stub, member):
    code, _ = rpc_code(
        stub.CreateUser, service_pb2.User(id="sneaky@example.com"), metadata=member
    )
    assert code == grpc.StatusCode.PERMISSION_DENIED


def test_create_duplicate_user_fails(stub, admin, member):
    reply = stub.CreateUser(service_pb2.User(id=MEMBER_ID), metadata=admin)
    assert reply.code == service_pb2.ERROR
    assert reply.reason == "Object already exists"


def test_member_can_read_self_but_not_others(stub, member, outsider):
    assert (
        stub.GetUser(service_pb2.GetRequest(id=MEMBER_ID), metadata=member).id
        == MEMBER_ID
    )
    code, _ = rpc_code(
        stub.GetUser, service_pb2.GetRequest(id=OUTSIDER_ID), metadata=member
    )
    assert code == grpc.StatusCode.PERMISSION_DENIED


def test_member_cannot_escalate_own_role_or_limits(stub, admin, member):
    me = stub.GetUser(service_pb2.GetRequest(id=MEMBER_ID), metadata=member)
    me.name = "Renamed Member"
    me.role = service_pb2.ADMIN
    me.rate_limits.daily_spend = 999999
    reply = stub.UpdateUser(me, metadata=member)
    assert reply.code == service_pb2.SUCCESS

    stored = stub.GetUser(service_pb2.GetRequest(id=MEMBER_ID), metadata=admin)
    assert stored.name == "Renamed Member"
    assert stored.role == service_pb2.NORMAL
    assert stored.rate_limits.daily_spend == 1000


def test_list_users_redacts_for_non_admins(stub, admin, member):
    as_admin = {
        u.id: u for u in stub.ListUsers(service_pb2.ListRequest(), metadata=admin)
    }
    as_member = {
        u.id: u for u in stub.ListUsers(service_pb2.ListRequest(), metadata=member)
    }

    assert {harness.ADMIN_ID, MEMBER_ID} <= as_admin.keys()
    assert as_admin[MEMBER_ID].rate_limits.daily_spend == 1000
    assert all(
        u.password_hash == "" and u.api_key.hash == "" for u in as_admin.values()
    )

    redacted = as_member[harness.ADMIN_ID]
    assert not redacted.HasField("api_key")
    assert not redacted.HasField("rate_limits")
    assert not redacted.HasField("last_login")


def test_admin_deletes_user(stub, admin):
    user_id = "contract-delete-me@example.com"
    assert (
        stub.CreateUser(service_pb2.User(id=user_id), metadata=admin).code
        == service_pb2.SUCCESS
    )

    reply = stub.DeleteUser(service_pb2.DeleteRequest(id=user_id), metadata=admin)

    assert reply.code == service_pb2.SUCCESS
    assert stub.GetUser(service_pb2.GetRequest(id=user_id), metadata=admin).id == ""
