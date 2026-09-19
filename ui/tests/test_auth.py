"""Tests for JWT auth flows."""

import time
from unittest import mock

import jwt as pyjwt
import pytest

from ui.tests.conftest import (
    _TEST_JWT_SECRET,
    _mint_jwt,
    _USER_EMAIL,
)


def test_create_and_decode_jwt():
    with mock.patch("ui.auth.MPAC_JWT_SECRET", _TEST_JWT_SECRET):
        from ui.auth import create_jwt, decode_jwt

        token = create_jwt(
            email=_USER_EMAIL,
            name="Test User",
            avatar_url="https://example.com/avatar.png",
            is_admin=False,
            org_domain="example.com",
        )
        ctx = decode_jwt(token)
        assert ctx.email == _USER_EMAIL
        assert ctx.name == "Test User"
        assert ctx.is_admin is False
        assert ctx.org_domain == "example.com"


def test_expired_jwt_raises():
    with mock.patch("ui.auth.MPAC_JWT_SECRET", _TEST_JWT_SECRET):
        from ui.auth import decode_jwt

        expired = _mint_jwt(_USER_EMAIL, exp_offset=-60)
        with pytest.raises(pyjwt.ExpiredSignatureError):
            decode_jwt(expired)


def test_invalid_jwt_raises():
    with mock.patch("ui.auth.MPAC_JWT_SECRET", _TEST_JWT_SECRET):
        from ui.auth import decode_jwt

        with pytest.raises(pyjwt.PyJWTError):
            decode_jwt("not.a.valid.jwt")


def test_reconnection_token_roundtrip():
    with mock.patch("ui.auth.MPAC_JWT_SECRET", _TEST_JWT_SECRET):
        from ui.auth import (
            create_reconnection_token,
            validate_reconnection_token,
        )

        token = create_reconnection_token("run-123", _USER_EMAIL)
        payload = validate_reconnection_token(token)
        assert payload["run_id"] == "run-123"
        assert payload["user_email"] == _USER_EMAIL
        assert payload["purpose"] == "stream_reconnection"


def test_reconnection_token_wrong_purpose():
    with mock.patch("ui.auth.MPAC_JWT_SECRET", _TEST_JWT_SECRET):
        from ui.auth import validate_reconnection_token

        tampered = pyjwt.encode(
            {
                "run_id": "x",
                "user_email": "a@b.com",
                "purpose": "evil",
                "exp": int(time.time()) + 3600,
            },
            _TEST_JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(pyjwt.InvalidTokenError):
            validate_reconnection_token(tampered)


@pytest.mark.asyncio
async def test_login_page_accessible_without_auth(anon_client):
    r = await anon_client.get("/login")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_logout_clears_cookie(client):
    r = await client.get("/logout", follow_redirects=False)
    assert r.status_code == 200
    cookie_header = r.headers.get("set-cookie", "")
    assert "mpac_jwt" in cookie_header
    assert (
        "Max-Age=0" in cookie_header
        or "expires=Thu, 01 Jan 1970" in cookie_header.lower()
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
