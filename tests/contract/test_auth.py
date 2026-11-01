import grpc
import harness
import pytest
from conftest import MEMBER_ID, rpc_code
from grpc_health.v1 import health_pb2, health_pb2_grpc

from proto import service_pb2


@pytest.mark.parametrize(
    "metadata, details",
    [
        ((), "Missing credentials."),
        ((("x-api-key", "sk_not_a_real_key"),), "Invalid API key."),
        (harness.jwt_metadata("someone@example.com", expires_in=-60), "JWT expired."),
        (harness.jwt_metadata("someone@example.com", audience="other"), "Invalid JWT."),
        (
            harness.jwt_metadata("someone@example.com", secret="wrong-secret"),
            "Invalid JWT.",
        ),
        (harness.jwt_metadata(None), "JWT missing sub claim."),
    ],
    ids=[
        "none",
        "bad_api_key",
        "expired_jwt",
        "wrong_audience",
        "wrong_secret",
        "no_sub",
    ],
)
def test_rejects_bad_credentials(stub, metadata, details):
    code, message = rpc_code(
        stub.ListTests, service_pb2.ListRequest(), metadata=metadata
    )
    assert code == grpc.StatusCode.UNAUTHENTICATED
    assert message == details


@pytest.mark.parametrize("audience", ["mpac-web", "mpac-system"])
def test_accepts_jwt_audiences(stub, admin, audience):
    metadata = harness.jwt_metadata(harness.ADMIN_ID, audience=audience)
    user = stub.GetUser(service_pb2.GetRequest(id=harness.ADMIN_ID), metadata=metadata)
    assert user.id == harness.ADMIN_ID


def test_api_key_authenticates_onboarded_admin(stub, admin):
    user = stub.GetUser(service_pb2.GetRequest(id=harness.ADMIN_ID), metadata=admin)
    assert user.id == harness.ADMIN_ID
    assert user.role == service_pb2.ADMIN


@pytest.mark.parametrize(
    "metadata",
    [
        (("x-jwt-token", ""), ("x-api-key", harness.ADMIN_API_KEY)),
        (("x-api-key", "sk_not_a_real_key"), ("x-api-key", harness.ADMIN_API_KEY)),
    ],
    ids=["empty_jwt_falls_through", "last_duplicate_wins"],
)
def test_header_selection_matches_python(stub, metadata):
    user = stub.GetUser(service_pb2.GetRequest(id=harness.ADMIN_ID), metadata=metadata)
    assert user.id == harness.ADMIN_ID


def test_empty_credentials_are_missing(stub):
    code, message = rpc_code(
        stub.ListTests,
        service_pb2.ListRequest(),
        metadata=(("x-jwt-token", ""), ("x-api-key", "")),
    )
    assert code == grpc.StatusCode.UNAUTHENTICATED
    assert message == "Missing credentials."


def test_login_succeeds_without_credentials_and_strips_secrets(stub):
    user = stub.LoginUser(
        service_pb2.LoginRequest(
            user_id=harness.ADMIN_ID, password=harness.ADMIN_PASSWORD
        )
    )
    assert user.id == harness.ADMIN_ID
    assert user.password_hash == ""
    assert user.api_key.hash == ""


@pytest.mark.parametrize(
    "user_id, password, expected",
    [
        (harness.ADMIN_ID, "wrong-password", grpc.StatusCode.UNAUTHENTICATED),
        ("nobody@example.com", "whatever", grpc.StatusCode.UNAUTHENTICATED),
        ("", "", grpc.StatusCode.INVALID_ARGUMENT),
        (harness.ADMIN_ID, "x" * 256, grpc.StatusCode.INVALID_ARGUMENT),
    ],
    ids=["wrong_password", "unknown_user", "empty", "oversized"],
)
def test_login_failures(stub, user_id, password, expected):
    code, _ = rpc_code(
        stub.LoginUser, service_pb2.LoginRequest(user_id=user_id, password=password)
    )
    assert code == expected


def test_login_fails_for_user_without_password(stub, member):
    code, _ = rpc_code(
        stub.LoginUser, service_pb2.LoginRequest(user_id=MEMBER_ID, password="anything")
    )
    assert code == grpc.StatusCode.UNAUTHENTICATED


def test_health_check_is_public(server_address):
    with grpc.insecure_channel(server_address) as channel:
        response = health_pb2_grpc.HealthStub(channel).Check(
            health_pb2.HealthCheckRequest()
        )
    assert response.status == health_pb2.HealthCheckResponse.SERVING
