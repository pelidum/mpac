"""Tests for /settings routes."""

from unittest import mock

import pytest

from server import service_pb2


@pytest.mark.asyncio
async def test_settings_requires_auth(anon_client):
    r = await anon_client.get("/settings", follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.asyncio
async def test_settings_page(client, mock_stub):
    user_pb = service_pb2.User()
    user_pb.id = "user@example.com"
    user_pb.name = "Test User"
    mock_stub.GetUser.return_value = user_pb
    mock_stub.ListTests.return_value = iter([])
    mock_stub.ListTestRuns.return_value = iter([])

    r = await client.get("/settings")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_mint_api_key_returns_raw_key(client, mock_stub):
    user_pb = service_pb2.User()
    user_pb.id = "user@example.com"
    user_pb.name = "Test User"
    mock_stub.GetUser.return_value = user_pb
    mock_stub.UpdateUser.return_value = service_pb2.StatusReply(
        code=service_pb2.ResponseCode.SUCCESS
    )

    r = await client.post("/settings/mint_api_key")
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["raw_key"].startswith("sk_live_")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
