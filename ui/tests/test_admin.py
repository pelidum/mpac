"""Tests for /admin routes — auth gate and basic CRUD."""

import pytest

from server import service_pb2


@pytest.mark.asyncio
async def test_admin_requires_auth(anon_client):
    r = await anon_client.get("/admin", follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.asyncio
async def test_admin_forbidden_for_non_admin(client):
    r = await client.get("/admin", follow_redirects=False)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_admin_page_for_admin(admin_client, mock_stub):
    mock_stub.ListUsers.return_value = iter([])
    mock_stub.ListBackends.return_value = iter([])
    r = await admin_client.get("/admin")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_add_backend_forbidden_for_non_admin(client):
    r = await client.post("/admin/add_backend", json={"name": "test"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_add_backend_success(admin_client, mock_stub):
    response_pb = service_pb2.StatusReply(
        code=service_pb2.ResponseCode.SUCCESS, id="be-new"
    )
    mock_stub.CreateBackend.return_value = response_pb
    r = await admin_client.post(
        "/admin/add_backend", json={"name": "new-backend", "provider": "openai"}
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


@pytest.mark.asyncio
async def test_delete_user_forbidden_for_non_admin(client):
    r = await client.post("/admin/delete_user", json={"id": "user@example.com"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_user_success(admin_client, mock_stub):
    mock_stub.DeleteUser.return_value = service_pb2.StatusReply(
        code=service_pb2.ResponseCode.SUCCESS
    )
    r = await admin_client.post("/admin/delete_user", json={"id": "user@example.com"})
    assert r.status_code == 200
    assert r.json()["success"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
