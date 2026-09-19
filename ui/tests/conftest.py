"""Shared fixtures for MPAC UI tests."""

import base64
import json
import time
from unittest import mock

import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner

_TEST_JWT_SECRET = "test-jwt-secret-32-bytes-long-xxx"
_ADMIN_EMAIL = "admin@example.com"
_USER_EMAIL = "user@example.com"
_TEST_CSRF_TOKEN = "test-csrf-token-for-tests"


def _mint_jwt(email: str, is_admin: bool = False, exp_offset: int = 3600) -> str:
    payload = {
        "sub": email,
        "name": "Test User",
        "avatar_url": "",
        "is_admin": is_admin,
        "org_domain": "example.com",
        "exp": int(time.time()) + exp_offset,
        "iat": int(time.time()),
        "aud": "mpac-web",
    }
    return pyjwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


@pytest.fixture()
def valid_jwt():
    return _mint_jwt(_USER_EMAIL)


@pytest.fixture()
def admin_jwt():
    return _mint_jwt(_ADMIN_EMAIL, is_admin=True)


@pytest.fixture()
def expired_jwt():
    return _mint_jwt(_USER_EMAIL, exp_offset=-60)


@pytest.fixture()
def mock_stub():
    stub = mock.MagicMock()
    stub.ListTestRuns.return_value = iter([])
    stub.ListTests.return_value = iter([])
    stub.ListModels.return_value = iter([])
    stub.ListUsers.return_value = iter([])
    stub.ListBackends.return_value = iter([])
    stub.ListBenchmarks.return_value = iter([])
    return stub


def _make_session_cookie(csrf_token: str = _TEST_CSRF_TOKEN) -> str:
    signer = TimestampSigner(_TEST_JWT_SECRET)
    data = base64.b64encode(json.dumps({"csrf_token": csrf_token}).encode())
    return signer.sign(data).decode("utf-8")


@pytest_asyncio.fixture()
async def client(valid_jwt, mock_stub):
    with (
        mock.patch("ui.auth.MPAC_JWT_SECRET", _TEST_JWT_SECRET),
        mock.patch("ui.grpc_client.MPAC_JWT_SECRET", _TEST_JWT_SECRET),
        # Patch the stub object itself (read by get_mpac_stub at call time):
        # routers bind get_mpac_stub at first import, so patching the function
        # would freeze every later test onto the first test's mock.
        mock.patch(
            "ui.grpc_client._stub",
            mock_stub,
        ),
    ):
        from ui.server import create_app

        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={"mpac_jwt": valid_jwt, "session": _make_session_cookie()},
            headers={"X-CSRF-Token": _TEST_CSRF_TOKEN},
        ) as ac:
            yield ac


@pytest_asyncio.fixture()
async def admin_client(admin_jwt, mock_stub):
    with (
        mock.patch("ui.auth.MPAC_JWT_SECRET", _TEST_JWT_SECRET),
        mock.patch("ui.grpc_client.MPAC_JWT_SECRET", _TEST_JWT_SECRET),
        # Patch the stub object itself (read by get_mpac_stub at call time):
        # routers bind get_mpac_stub at first import, so patching the function
        # would freeze every later test onto the first test's mock.
        mock.patch(
            "ui.grpc_client._stub",
            mock_stub,
        ),
    ):
        from ui.server import create_app

        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={"mpac_jwt": admin_jwt, "session": _make_session_cookie()},
            headers={"X-CSRF-Token": _TEST_CSRF_TOKEN},
        ) as ac:
            yield ac


@pytest_asyncio.fixture()
async def anon_client(mock_stub):
    with (
        mock.patch("ui.auth.MPAC_JWT_SECRET", _TEST_JWT_SECRET),
        mock.patch("ui.grpc_client.MPAC_JWT_SECRET", _TEST_JWT_SECRET),
        # Patch the stub object itself (read by get_mpac_stub at call time):
        # routers bind get_mpac_stub at first import, so patching the function
        # would freeze every later test onto the first test's mock.
        mock.patch(
            "ui.grpc_client._stub",
            mock_stub,
        ),
    ):
        from ui.server import create_app

        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac
