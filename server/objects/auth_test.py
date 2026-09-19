"""Tests for ApiAuthClient.

Covers JWT authentication (used by the FastAPI UI), API-key authentication,
public-method bypass, and rejection of unknown/removed auth schemes.
"""

import asyncio
import datetime
import time
import unittest.mock as mock

import grpc
import jwt as pyjwt
import pytest

from server.objects.auth import (
    ApiAuthClient,
    USER_EMAIL_CONTEXT,
    WEB_UI_CONTEXT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_USER = "admin@example.com"


def _make_handler_call_details(
    metadata: dict,
    method: str = "/pelidum.services.grpc.mpac.MPAC/ListModels",
) -> mock.MagicMock:
    hcd = mock.MagicMock(spec=grpc.HandlerCallDetails)
    hcd.invocation_metadata = list(metadata.items())
    hcd.method = method
    return hcd


def _make_stub_handler():
    """Returns a minimal RpcMethodHandler stub that records whether it was invoked.

    _replace mirrors namedtuple semantics (a copy with fields swapped) so the
    interceptor's handler-rewriting in _abort actually takes effect under test.
    """
    handler = mock.MagicMock()
    handler.unary_unary = mock.AsyncMock(return_value=mock.MagicMock())
    handler.unary_stream = None
    handler.stream_unary = None
    handler.stream_stream = None

    def _replace(**kw):
        new = mock.MagicMock()
        new.unary_unary = kw.get("unary_unary", handler.unary_unary)
        new.unary_stream = kw.get("unary_stream", handler.unary_stream)
        new.stream_unary = kw.get("stream_unary", handler.stream_unary)
        new.stream_stream = kw.get("stream_stream", handler.stream_stream)
        new._replace = _replace
        return new

    handler._replace = _replace
    return handler


# ---------------------------------------------------------------------------
# Removed auth schemes
# ---------------------------------------------------------------------------


class TestRemovedServiceKeyAuth:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_service_key_header_is_ignored(self):
        """x-service-key with no JWT/API-key must be rejected as missing credentials."""
        abort_called = False
        abort_message = None

        async def run():
            nonlocal abort_called, abort_message
            db_pool = mock.AsyncMock()
            interceptor = ApiAuthClient(db_pool=db_pool)

            hcd = _make_handler_call_details(
                {"x-service-key": "some-key", "x-session-user": _VALID_USER}
            )

            async def fake_continuation(hcd):
                return _make_stub_handler()

            original_abort = interceptor._abort

            async def spy_abort(
                continuation, hcd, message, code=grpc.StatusCode.UNAUTHENTICATED
            ):
                nonlocal abort_called, abort_message
                abort_called = True
                abort_message = message
                return await original_abort(continuation, hcd, message, code)

            interceptor._abort = spy_abort
            await interceptor.intercept_service(fake_continuation, hcd)

        self._run(run())
        assert abort_called
        assert abort_message == "Missing credentials."

    def test_login_user_bypasses_auth(self):
        """LoginUser must reach continuation even with no credentials."""
        continuation_called = False

        async def run():
            nonlocal continuation_called
            db_pool = mock.AsyncMock()
            interceptor = ApiAuthClient(db_pool=db_pool)

            hcd = _make_handler_call_details(
                {},
                method="/pelidum.services.grpc.mpac.MPAC/LoginUser",
            )

            async def fake_continuation(hcd):
                nonlocal continuation_called
                continuation_called = True
                return _make_stub_handler()

            await interceptor.intercept_service(fake_continuation, hcd)

        self._run(run())
        assert continuation_called, "LoginUser must bypass auth interceptor"

    def test_missing_all_credentials_is_rejected(self):
        """Requests with no credentials at all must be rejected."""
        abort_called = False

        async def run():
            nonlocal abort_called
            db_pool = mock.AsyncMock()
            interceptor = ApiAuthClient(db_pool=db_pool)

            hcd = _make_handler_call_details({})

            async def fake_continuation(hcd):
                return _make_stub_handler()

            original_abort = interceptor._abort

            async def spy_abort(
                continuation, hcd, message, code=grpc.StatusCode.UNAUTHENTICATED
            ):
                nonlocal abort_called
                abort_called = True
                return await original_abort(continuation, hcd, message, code)

            interceptor._abort = spy_abort
            await interceptor.intercept_service(fake_continuation, hcd)

        self._run(run())
        assert abort_called


# ---------------------------------------------------------------------------
# JWT authentication
# ---------------------------------------------------------------------------

_VALID_JWT_SECRET = "test-jwt-secret-for-tests-only"
_JWT_USER = "user@example.com"


def _mint_jwt(
    secret: str = _VALID_JWT_SECRET,
    sub: str = _JWT_USER,
    exp_delta: int = 3600,
    aud: str = "mpac-web",
) -> str:
    payload = {
        "sub": sub,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
        "aud": aud,
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


class TestJWTBranch:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_valid_jwt_reaches_continuation(self):
        continuation_called = False

        async def run():
            nonlocal continuation_called
            db_pool = mock.AsyncMock()
            interceptor = ApiAuthClient(db_pool=db_pool)
            token = _mint_jwt()
            hcd = _make_handler_call_details({"x-jwt-token": token})

            async def fake_continuation(hcd):
                nonlocal continuation_called
                continuation_called = True
                return _make_stub_handler()

            with mock.patch(
                "server.objects.auth.MPAC_JWT_SECRET",
                _VALID_JWT_SECRET,
            ):
                await interceptor.intercept_service(fake_continuation, hcd)

        self._run(run())
        assert continuation_called

    def test_valid_jwt_sets_user_email_context(self):
        async def run():
            db_pool = mock.AsyncMock()
            interceptor = ApiAuthClient(db_pool=db_pool)
            token = _mint_jwt(sub=_JWT_USER)
            hcd = _make_handler_call_details({"x-jwt-token": token})

            async def fake_continuation(hcd):
                return _make_stub_handler()

            with mock.patch(
                "server.objects.auth.MPAC_JWT_SECRET",
                _VALID_JWT_SECRET,
            ):
                await interceptor.intercept_service(fake_continuation, hcd)
            return USER_EMAIL_CONTEXT.get()

        result = self._run(run())
        assert result == _JWT_USER

    def test_expired_jwt_is_rejected(self):
        abort_called = False

        async def run():
            nonlocal abort_called
            db_pool = mock.AsyncMock()
            interceptor = ApiAuthClient(db_pool=db_pool)
            token = _mint_jwt(exp_delta=-1)
            hcd = _make_handler_call_details({"x-jwt-token": token})

            async def fake_continuation(hcd):
                return _make_stub_handler()

            original_abort = interceptor._abort

            async def spy_abort(
                continuation, hcd, message, code=grpc.StatusCode.UNAUTHENTICATED
            ):
                nonlocal abort_called
                abort_called = True
                return await original_abort(continuation, hcd, message, code)

            interceptor._abort = spy_abort

            with mock.patch(
                "server.objects.auth.MPAC_JWT_SECRET",
                _VALID_JWT_SECRET,
            ):
                await interceptor.intercept_service(fake_continuation, hcd)

        self._run(run())
        assert abort_called

    def test_jwt_with_wrong_secret_is_rejected(self):
        abort_called = False

        async def run():
            nonlocal abort_called
            db_pool = mock.AsyncMock()
            interceptor = ApiAuthClient(db_pool=db_pool)
            token = _mint_jwt(secret="wrong-secret")
            hcd = _make_handler_call_details({"x-jwt-token": token})

            async def fake_continuation(hcd):
                return _make_stub_handler()

            original_abort = interceptor._abort

            async def spy_abort(
                continuation, hcd, message, code=grpc.StatusCode.UNAUTHENTICATED
            ):
                nonlocal abort_called
                abort_called = True
                return await original_abort(continuation, hcd, message, code)

            interceptor._abort = spy_abort

            with mock.patch(
                "server.objects.auth.MPAC_JWT_SECRET",
                _VALID_JWT_SECRET,
            ):
                await interceptor.intercept_service(fake_continuation, hcd)

        self._run(run())
        assert abort_called

    def test_jwt_missing_sub_is_rejected(self):
        abort_called = False

        async def run():
            nonlocal abort_called
            db_pool = mock.AsyncMock()
            interceptor = ApiAuthClient(db_pool=db_pool)
            payload = {
                "exp": int(time.time()) + 3600,
                "iat": int(time.time()),
                "aud": "mpac-web",
            }
            token = pyjwt.encode(payload, _VALID_JWT_SECRET, algorithm="HS256")
            hcd = _make_handler_call_details({"x-jwt-token": token})

            async def fake_continuation(hcd):
                return _make_stub_handler()

            original_abort = interceptor._abort

            async def spy_abort(
                continuation, hcd, message, code=grpc.StatusCode.UNAUTHENTICATED
            ):
                nonlocal abort_called
                abort_called = True
                return await original_abort(continuation, hcd, message, code)

            interceptor._abort = spy_abort

            with mock.patch(
                "server.objects.auth.MPAC_JWT_SECRET",
                _VALID_JWT_SECRET,
            ):
                await interceptor.intercept_service(fake_continuation, hcd)

        self._run(run())
        assert abort_called

    def test_jwt_unconfigured_is_rejected(self):
        """If MPAC_JWT_SECRET is not set, JWT tokens are rejected even if well-formed."""
        abort_called = False

        async def run():
            nonlocal abort_called
            db_pool = mock.AsyncMock()
            interceptor = ApiAuthClient(db_pool=db_pool)
            token = _mint_jwt()
            hcd = _make_handler_call_details({"x-jwt-token": token})

            async def fake_continuation(hcd):
                return _make_stub_handler()

            original_abort = interceptor._abort

            async def spy_abort(
                continuation, hcd, message, code=grpc.StatusCode.UNAUTHENTICATED
            ):
                nonlocal abort_called
                abort_called = True
                return await original_abort(continuation, hcd, message, code)

            interceptor._abort = spy_abort

            with mock.patch(
                "server.objects.auth.MPAC_JWT_SECRET",
                None,
            ):
                await interceptor.intercept_service(fake_continuation, hcd)

        self._run(run())
        assert abort_called


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
