"""Tests for CSRF protection."""

import pytest

from ui.tests.conftest import _USER_EMAIL


@pytest.mark.asyncio
async def test_post_without_csrf_token_returns_403(client):
    r = await client.post(
        "/settings/mint_api_key",
        headers={"X-CSRF-Token": ""},
        follow_redirects=False,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_post_with_valid_csrf_token_succeeds(client, mock_stub):
    from server import service_pb2

    mock_stub.GetUser.return_value = service_pb2.User(id=_USER_EMAIL, name="Test User")
    mock_stub.MintApiKey.return_value = service_pb2.User(
        id=_USER_EMAIL, name="Test User"
    )

    r = await client.get("/settings")
    session_cookie = r.cookies.get("session")

    csrf_token = None
    for line in r.text.splitlines():
        if 'name="csrf-token"' in line:
            start = line.index('content="') + len('content="')
            end = line.index('"', start)
            csrf_token = line[start:end]
            break

    assert csrf_token, "CSRF token not found in page meta tag"

    cookies = {"session": session_cookie} if session_cookie else {}
    r = await client.post(
        "/settings/mint_api_key",
        headers={"X-CSRF-Token": csrf_token},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code != 403


@pytest.mark.asyncio
async def test_post_with_wrong_csrf_token_returns_403(client, mock_stub):
    from server import service_pb2

    mock_stub.GetUser.return_value = service_pb2.User(id=_USER_EMAIL, name="Test User")

    r = await client.get("/settings")
    session_cookie = r.cookies.get("session")
    cookies = {"session": session_cookie} if session_cookie else {}

    r = await client.post(
        "/settings/mint_api_key",
        headers={"X-CSRF-Token": "wrong-token"},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_login_exempt_from_csrf(anon_client, mock_stub):
    from server import service_pb2

    mock_stub.LoginUser.return_value = service_pb2.User()

    r = await anon_client.post(
        "/login/password",
        data={"username": "test", "password": "test"},
        follow_redirects=False,
    )
    assert r.status_code != 403


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
