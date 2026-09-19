"""Unit tests for AnswersMixin: default limit guard and proto deserialization."""

import asyncio
import unittest.mock as mock

import pytest

from server import service_pb2
from server.objects.answers import AnswersMixin


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_context():
    ctx = mock.AsyncMock()

    async def abort(code, msg):
        raise Exception(msg)

    ctx.abort = abort
    return ctx


def _stub_with_conn(conn):
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)

    class _Stub(AnswersMixin):
        db_pool = pool

        async def get_request_owner(self, request):
            return "admin@test.com"

        async def is_user_admin(self, user_id):
            return True

        async def _grpc_get(self, id, obj_type, obj_owner=None):
            return b"\x01"

    return _Stub()


# ---------------------------------------------------------------------------
# ListTestRunAnswers
# ---------------------------------------------------------------------------


class TestListTestRunAnswersLimit:
    def _captured_stub(self):
        """Returns (stub, captured_args_list). captured_args_list is populated
        with the positional args passed to conn.fetch on each call."""
        captured = {}
        conn = mock.AsyncMock()

        async def fake_fetch(query, *args):
            captured["args"] = args
            return []

        conn.fetch = fake_fetch
        return _stub_with_conn(conn), captured

    def test_zero_limit_defaults_to_fifty(self):
        stub, captured = self._captured_stub()
        _run(self._drain(stub, parent_id="run-1", limit=0))
        # query_values = (parent_id, num_items); num_items should be 50
        assert captured["args"][1] == 50

    def test_nonzero_limit_passed_through(self):
        stub, captured = self._captured_stub()
        _run(self._drain(stub, parent_id="run-1", limit=10))
        assert captured["args"][1] == 10

    def test_parent_id_passed_as_first_arg(self):
        stub, captured = self._captured_stub()
        _run(self._drain(stub, parent_id="my-run-id", limit=5))
        assert captured["args"][0] == "my-run-id"

    async def _drain(self, stub, parent_id, limit):
        results = []
        async for a in stub.ListTestRunAnswers(
            request=service_pb2.ListRequest(parent_id=parent_id, limit=limit),
            context=_make_context(),
        ):
            results.append(a)
        return results


class TestListTestRunAnswersDeserialization:
    def test_yields_parsed_protos(self):
        answer = service_pb2.TestRunAnswer()
        answer.id = "ans-1"
        answer.run_id = "run-1"
        answer.answer = "A"

        conn = mock.AsyncMock()
        conn.fetch = mock.AsyncMock(
            return_value=[{"proto_bytes": answer.SerializeToString()}]
        )
        stub = _stub_with_conn(conn)

        async def run():
            results = []
            async for a in stub.ListTestRunAnswers(
                request=service_pb2.ListRequest(parent_id="run-1", limit=5),
                context=_make_context(),
            ):
                results.append(a)
            return results

        results = _run(run())
        assert len(results) == 1
        assert results[0].id == "ans-1"
        assert results[0].run_id == "run-1"
        assert results[0].answer == "A"

    def test_empty_result_yields_nothing(self):
        conn = mock.AsyncMock()
        conn.fetch = mock.AsyncMock(return_value=[])
        stub = _stub_with_conn(conn)

        async def run():
            results = []
            async for a in stub.ListTestRunAnswers(
                request=service_pb2.ListRequest(parent_id="run-1", limit=5),
                context=_make_context(),
            ):
                results.append(a)
            return results

        results = _run(run())
        assert results == []


# ---------------------------------------------------------------------------
# ListTestRunAnswers — parent-Test visibility enforcement
# ---------------------------------------------------------------------------
#
# TestRun/TestRunAnswer have no visibility field of their own, so the generic
# visibility clause inside _grpc_get(obj_type="test_runs") is always a no-op
# (proto_jsonb->'visibility' is always NULL on that table). Without an
# explicit re-check against the parent Test, any authenticated non-admin user
# could read the answers for a run whose parent Test is owner-only or
# specified-users. These tests lock in the fix.


def _stub_with_visibility(
    request_owner="user@example.com",
    is_admin=False,
    run_owner="creator@example.com",
    run_test_id="test-1",
    run_exists=True,
    parent_test_visible=True,
    fetch_result=None,
):
    conn = mock.AsyncMock()
    conn.fetch = mock.AsyncMock(return_value=fetch_result or [])

    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)

    run_pb = service_pb2.TestRun()
    run_pb.id = "run-1"
    run_pb.owner = run_owner
    run_pb.test_id = run_test_id

    class _Stub(AnswersMixin):
        db_pool = pool

        async def get_request_owner(self, request):
            return request_owner

        async def is_user_admin(self, user_id):
            return is_admin

        async def _grpc_get(self, id, obj_type, obj_owner=None):
            if obj_type == "test_runs":
                return run_pb.SerializeToString() if run_exists else None
            if obj_type == "tests":
                return b"\x01" if parent_test_visible else None
            return None

    return _Stub(), conn


async def _drain_answers(stub, parent_id="run-1", limit=5):
    results = []
    async for a in stub.ListTestRunAnswers(
        request=service_pb2.ListRequest(parent_id=parent_id, limit=limit),
        context=_make_context(),
    ):
        results.append(a)
    return results


class TestListTestRunAnswersVisibility:
    # -- Happy paths -----------------------------------------------------

    def test_admin_bypasses_visibility_check(self):
        """Admins skip the parent-Test check entirely."""
        stub, conn = _stub_with_visibility(is_admin=True, parent_test_visible=False)
        _run(_drain_answers(stub))
        assert conn.fetch.await_count == 1

    def test_visible_parent_test_allows_non_owner(self):
        """A non-owner requester who can see the parent Test (public /
        specified-users / org-only) may read the run's answers."""
        stub, conn = _stub_with_visibility(
            request_owner="viewer@example.com",
            run_owner="creator@example.com",
            parent_test_visible=True,
        )
        _run(_drain_answers(stub))
        assert conn.fetch.await_count == 1

    def test_run_owner_can_view_own_answers_even_if_test_now_hidden(self):
        """The run's own owner can always read their run's answers, even if
        the parent Test's visibility was later restricted against them."""
        stub, conn = _stub_with_visibility(
            request_owner="creator@example.com",
            run_owner="creator@example.com",
            parent_test_visible=False,
        )
        _run(_drain_answers(stub))
        assert conn.fetch.await_count == 1

    def test_orphaned_run_still_visible_to_its_owner(self):
        """Parent Test was deleted (_grpc_get('tests') -> None); the run's
        owner must still see their own orphaned run's answers, mirroring
        ListTestRuns' orphan-run fallback."""
        stub, conn = _stub_with_visibility(
            request_owner="creator@example.com",
            run_owner="creator@example.com",
            run_test_id="deleted-test-id",
            parent_test_visible=False,
        )
        _run(_drain_answers(stub))
        assert conn.fetch.await_count == 1

    # -- Sad paths ---------------------------------------------------------

    def test_hidden_parent_test_denies_non_owner(self):
        """The actual security fix: a non-admin, non-owner requester must NOT
        be able to read answers for a run whose parent Test they can't see
        (e.g. an owner-only or specified-users test)."""
        stub, conn = _stub_with_visibility(
            request_owner="attacker@example.com",
            run_owner="creator@example.com",
            parent_test_visible=False,
        )
        results = _run(_drain_answers(stub))
        assert results == []
        assert conn.fetch.await_count == 0

    def test_orphaned_run_denied_to_non_owner(self):
        """Parent Test deleted, requester is NOT the run owner -- must not
        fall back to allowing access."""
        stub, conn = _stub_with_visibility(
            request_owner="attacker@example.com",
            run_owner="creator@example.com",
            run_test_id="deleted-test-id",
            parent_test_visible=False,
        )
        results = _run(_drain_answers(stub))
        assert results == []
        assert conn.fetch.await_count == 0

    def test_run_not_found_denies_access(self):
        """The run itself doesn't exist (or isn't visible at all) -> no
        answers and no DB query for answers is issued."""
        stub, conn = _stub_with_visibility(run_exists=False)
        results = _run(_drain_answers(stub))
        assert results == []
        assert conn.fetch.await_count == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
