"""Unit tests for TestItemsMixin: BatchCreateTestItems grouping, permission, and counting."""

import asyncio
import unittest.mock as mock

import pytest

from server import service_pb2
from server.objects.test_items import TestItemsMixin


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


_ctx = mock.AsyncMock()


class _StubTestItems(TestItemsMixin):
    _request_owner = "user@example.com"
    _is_admin = False
    _test_owners: dict = {}

    def __init__(self):
        # Mock pool for the bulk-insert path (executemany + counter update).
        conn = mock.AsyncMock()
        pool = mock.MagicMock()
        pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        self.db_pool = pool
        self._db_conn = conn

    async def get_request_owner(self, request):
        return self._request_owner

    async def is_user_admin(self, user_id):
        return self._is_admin

    async def GetTest(self, request, context):
        test_pb = service_pb2.Test()
        test_pb.id = request.id
        test_pb.owner = self._test_owners.get(request.id, "other@example.com")
        return test_pb

    async def _grpc_create(self, id, proto_obj, obj_type, overwrite=False):
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)


def _make_item(test_id, question="Q?"):
    item = service_pb2.TestItem()
    item.test_id = test_id
    item.question = question
    item.choices.extend(["A", "B"])
    item.answer = "A"
    return item


# ---------------------------------------------------------------------------
# BatchCreateTestItems
# ---------------------------------------------------------------------------


class TestBatchCreateTestItems:
    def test_all_succeed_when_requester_is_owner(self):
        stub = _StubTestItems()
        stub._request_owner = "user@example.com"
        stub._test_owners = {"test-1": "user@example.com"}

        request = service_pb2.BatchCreateTestItemsRequest(
            items=[
                _make_item("test-1"),
                _make_item("test-1"),
            ]
        )
        result = _run(stub.BatchCreateTestItems(request=request, context=_ctx))
        assert result.successful_count == 2
        assert result.failed_count == 0

    def test_all_fail_when_requester_is_not_owner(self):
        stub = _StubTestItems()
        stub._request_owner = "user@example.com"
        stub._test_owners = {"test-1": "other@example.com"}

        request = service_pb2.BatchCreateTestItemsRequest(
            items=[
                _make_item("test-1"),
            ]
        )
        result = _run(stub.BatchCreateTestItems(request=request, context=_ctx))
        assert result.failed_count == 1
        assert result.successful_count == 0

    def test_admin_bypasses_ownership_check(self):
        stub = _StubTestItems()
        stub._request_owner = "admin@example.com"
        stub._is_admin = True
        stub._test_owners = {"test-1": "other@example.com"}

        request = service_pb2.BatchCreateTestItemsRequest(
            items=[
                _make_item("test-1"),
                _make_item("test-1"),
            ]
        )
        result = _run(stub.BatchCreateTestItems(request=request, context=_ctx))
        assert result.successful_count == 2
        assert result.failed_count == 0

    def test_mixed_ownership_counted_correctly(self):
        stub = _StubTestItems()
        stub._request_owner = "user@example.com"
        stub._test_owners = {
            "test-owned": "user@example.com",
            "test-other": "other@example.com",
        }

        request = service_pb2.BatchCreateTestItemsRequest(
            items=[
                _make_item("test-owned"),
                _make_item("test-owned"),
                _make_item("test-other"),
            ]
        )
        result = _run(stub.BatchCreateTestItems(request=request, context=_ctx))
        assert result.successful_count == 2
        assert result.failed_count == 1

    def test_result_list_length_matches_item_count(self):
        stub = _StubTestItems()
        stub._request_owner = "user@example.com"
        stub._test_owners = {"test-1": "user@example.com"}

        request = service_pb2.BatchCreateTestItemsRequest(
            items=[_make_item("test-1") for _ in range(5)]
        )
        result = _run(stub.BatchCreateTestItems(request=request, context=_ctx))
        assert len(result.results) == 5

    def test_empty_batch_returns_zero_counts(self):
        stub = _StubTestItems()
        request = service_pb2.BatchCreateTestItemsRequest(items=[])
        result = _run(stub.BatchCreateTestItems(request=request, context=_ctx))
        assert result.successful_count == 0
        assert result.failed_count == 0
        assert len(result.results) == 0

    def test_failed_result_carries_test_id(self):
        stub = _StubTestItems()
        stub._request_owner = "user@example.com"
        stub._test_owners = {"test-1": "other@example.com"}

        request = service_pb2.BatchCreateTestItemsRequest(items=[_make_item("test-1")])
        result = _run(stub.BatchCreateTestItems(request=request, context=_ctx))
        assert result.results[0].code == service_pb2.ResponseCode.ERROR
        assert result.results[0].id == "test-1"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
