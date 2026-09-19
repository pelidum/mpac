"""Unit tests for TestsMixin: UpdateTest ACL enforcement and field preservation."""

import asyncio
import unittest.mock as mock

import grpc
import pytest

from server import service_pb2
from server.objects.tests import TestsMixin


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Aborted(BaseException):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _make_context():
    ctx = mock.AsyncMock()

    async def abort(code, msg):
        raise _Aborted(code, msg)

    ctx.abort = abort
    return ctx


class _StubTests(TestsMixin):
    _request_owner = "user@example.com"
    _is_admin = False
    _existing_test_owner = "user@example.com"

    async def get_request_owner(self, request):
        return self._request_owner

    async def is_user_admin(self, user_id):
        return self._is_admin

    async def GetTest(self, request, context):
        test_pb = service_pb2.Test()
        test_pb.id = request.id
        test_pb.owner = self._existing_test_owner
        test_pb.name = "original name"
        test_pb.created_at_utc.GetCurrentTime()
        return test_pb

    async def _grpc_update(self, id, proto_obj, obj_type, obj_owner=None):
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)


# ---------------------------------------------------------------------------
# UpdateTest — ACL
# ---------------------------------------------------------------------------


class TestUpdateTestACL:
    def test_owner_can_update(self):
        stub = _StubTests()
        stub._request_owner = "user@example.com"
        stub._existing_test_owner = "user@example.com"

        result = _run(
            stub.UpdateTest(
                request=service_pb2.Test(id="test-1", name="new name"),
                context=_make_context(),
            )
        )
        assert result.code == service_pb2.ResponseCode.SUCCESS

    def test_non_owner_is_denied(self):
        stub = _StubTests()
        stub._request_owner = "other@example.com"
        stub._existing_test_owner = "user@example.com"
        stub._is_admin = False

        with pytest.raises(_Aborted) as exc_info:
            _run(
                stub.UpdateTest(
                    request=service_pb2.Test(id="test-1", name="attempted update"),
                    context=_make_context(),
                )
            )
        assert exc_info.value.code == grpc.StatusCode.PERMISSION_DENIED

    def test_admin_can_update_any_test(self):
        stub = _StubTests()
        stub._request_owner = "admin@example.com"
        stub._existing_test_owner = "user@example.com"
        stub._is_admin = True

        result = _run(
            stub.UpdateTest(
                request=service_pb2.Test(id="test-1", name="admin update"),
                context=_make_context(),
            )
        )
        assert result.code == service_pb2.ResponseCode.SUCCESS


# ---------------------------------------------------------------------------
# UpdateTest — field preservation
# ---------------------------------------------------------------------------


class TestUpdateTestFieldPreservation:
    def test_original_owner_is_not_overwritten(self):
        """A caller must not be able to reassign ownership via UpdateTest."""
        captured = {}
        stub = _StubTests()
        stub._request_owner = "user@example.com"
        stub._existing_test_owner = "user@example.com"

        async def spy_update(id, proto_obj, obj_type, obj_owner=None):
            captured["owner"] = proto_obj.owner
            return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

        stub._grpc_update = spy_update

        _run(
            stub.UpdateTest(
                request=service_pb2.Test(
                    id="test-1", name="new name", owner="attacker@example.com"
                ),
                context=_make_context(),
            )
        )
        assert captured["owner"] == "user@example.com"

    def test_original_created_at_is_preserved(self):
        """UpdateTest must copy created_at_utc from the original, not reset it."""
        captured = {}
        stub = _StubTests()
        stub._request_owner = "user@example.com"
        stub._existing_test_owner = "user@example.com"

        async def spy_update(id, proto_obj, obj_type, obj_owner=None):
            captured["created_at"] = proto_obj.created_at_utc.seconds
            return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

        stub._grpc_update = spy_update

        # Capture what GetTest returns so we know the expected timestamp
        original_created_at = None

        original_get = stub.GetTest

        async def recording_get(request, context):
            pb = await original_get(request, context)
            nonlocal original_created_at
            original_created_at = pb.created_at_utc.seconds
            return pb

        stub.GetTest = recording_get

        _run(
            stub.UpdateTest(
                request=service_pb2.Test(id="test-1", name="new name"),
                context=_make_context(),
            )
        )
        assert captured["created_at"] == original_created_at

    def test_modified_at_is_refreshed(self):
        """UpdateTest must set a fresh modified_at_utc."""
        captured = {}
        stub = _StubTests()
        stub._request_owner = "user@example.com"
        stub._existing_test_owner = "user@example.com"

        async def spy_update(id, proto_obj, obj_type, obj_owner=None):
            captured["modified_at"] = proto_obj.modified_at_utc.seconds
            return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

        stub._grpc_update = spy_update

        _run(
            stub.UpdateTest(
                request=service_pb2.Test(id="test-1", name="updated"),
                context=_make_context(),
            )
        )
        assert captured["modified_at"] > 0


# ---------------------------------------------------------------------------
# Read authorization (C1 IDOR fix)
# ---------------------------------------------------------------------------


def _make_pool_with_test(test_pb, *, visible=True):
    """Return a mock db_pool that returns test_pb from queries (or None if not visible)."""
    proto_bytes = test_pb.SerializeToString() if visible else None
    item_count = test_pb.item_count if test_pb else 0
    conn = mock.AsyncMock()
    conn.fetchrow = mock.AsyncMock(
        return_value={"proto_bytes": proto_bytes, "item_count": item_count}
        if proto_bytes
        else None
    )
    conn.fetch = mock.AsyncMock(
        return_value=[{"proto_bytes": proto_bytes, "item_count": item_count}]
        if proto_bytes
        else []
    )
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


class _StubTestsWithDB(TestsMixin):
    _request_owner = "user@example.com"
    _is_admin = False

    async def get_request_owner(self, request):
        return self._request_owner

    async def is_user_admin(self, user_id):
        return self._is_admin

    def _visibility_where_clause(self, requesting_user, is_admin, param_offset=0):
        if is_admin:
            return ("TRUE", [])
        p = param_offset + 1
        return (
            f"(proto_jsonb->>'owner' = ${p} OR proto_jsonb->'visibility' IS NULL"
            f" OR proto_jsonb->'visibility'->>'type' = 'VISIBILITY_PUBLIC')",
            [requesting_user],
        )


class TestGetTestReadAuth:
    def test_owner_can_read_own_test(self):
        test_pb = service_pb2.Test(id="test-1", owner="user@example.com", name="t")
        stub = _StubTestsWithDB()
        stub._request_owner = "user@example.com"
        stub.db_pool = _make_pool_with_test(test_pb)

        result = _run(
            stub.GetTest(
                request=service_pb2.GetRequest(id="test-1"),
                context=_make_context(),
            )
        )
        assert result.id == "test-1"

    def test_admin_can_read_any_test(self):
        test_pb = service_pb2.Test(id="test-1", owner="other@example.com", name="t")
        stub = _StubTestsWithDB()
        stub._request_owner = "admin@example.com"
        stub._is_admin = True
        stub.db_pool = _make_pool_with_test(test_pb)

        result = _run(
            stub.GetTest(
                request=service_pb2.GetRequest(id="test-1"),
                context=_make_context(),
            )
        )
        assert result.id == "test-1"

    def test_non_owner_gets_aborted_for_invisible_test(self):
        test_pb = service_pb2.Test(id="test-1", owner="other@example.com", name="t")
        stub = _StubTestsWithDB()
        stub._request_owner = "attacker@example.com"
        stub._is_admin = False
        stub.db_pool = _make_pool_with_test(test_pb, visible=False)

        with pytest.raises(_Aborted):
            _run(
                stub.GetTest(
                    request=service_pb2.GetRequest(id="test-1"),
                    context=_make_context(),
                )
            )


class TestListTestsReadAuth:
    def test_admin_sees_all(self):
        test_pb = service_pb2.Test(id="test-1", owner="other@example.com", name="t")
        stub = _StubTestsWithDB()
        stub._request_owner = "admin@example.com"
        stub._is_admin = True
        stub.db_pool = _make_pool_with_test(test_pb)

        results = _run(self._drain(stub, limit=50))
        assert len(results) == 1

    def test_non_admin_gets_filtered_list(self):
        stub = _StubTestsWithDB()
        stub._request_owner = "user@example.com"
        stub._is_admin = False
        stub.db_pool = _make_pool_with_test(None, visible=False)

        results = _run(self._drain(stub, limit=50))
        assert len(results) == 0

    async def _drain(self, stub, limit):
        results = []
        async for t in stub.ListTests(
            request=service_pb2.ListRequest(limit=limit),
            context=_make_context(),
        ):
            results.append(t)
        return results


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
