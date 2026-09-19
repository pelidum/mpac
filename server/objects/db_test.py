"""Unit tests for DBMixin pure helpers: grading, validation, cost estimation."""

import asyncio
import unittest.mock as mock

import pytest
from werkzeug.security import check_password_hash

from server import service_pb2
from server.objects.db import DBMixin


class _StubDB(DBMixin):
    """Minimal concrete class so we can instantiate DBMixin without a real pool."""

    db_pool = None


@pytest.fixture
def db():
    return _StubDB()


# ---------------------------------------------------------------------------
# calculate_grade
# ---------------------------------------------------------------------------


class TestCalculateGrade:
    def test_high_quality(self, db):
        assert db.calculate_grade(0.95) == "⭐⭐⭐⭐⭐ High Quality"
        assert db.calculate_grade(1.0) == "⭐⭐⭐⭐⭐ High Quality"

    def test_ok_quality(self, db):
        assert db.calculate_grade(0.80) == "⭐⭐⭐⭐ OK Quality"
        assert db.calculate_grade(0.94) == "⭐⭐⭐⭐ OK Quality"

    def test_low_quality(self, db):
        assert db.calculate_grade(0.70) == "⭐⭐⭐ Low Quality"
        assert db.calculate_grade(0.79) == "⭐⭐⭐ Low Quality"

    def test_very_low_quality(self, db):
        assert db.calculate_grade(0.50) == "⭐⭐ Very Low Quality"
        assert db.calculate_grade(0.69) == "⭐⭐ Very Low Quality"

    def test_garbage(self, db):
        assert db.calculate_grade(0.0) == "⭐ Garbage"
        assert db.calculate_grade(0.49) == "⭐ Garbage"


# ---------------------------------------------------------------------------
# calculate_confidence
# ---------------------------------------------------------------------------


class TestCalculateConfidence:
    def test_unanimous(self, db):
        assert db.calculate_confidence(1.0) == "⭐⭐⭐⭐⭐ Unanimous consensus"

    def test_strong(self, db):
        assert db.calculate_confidence(0.75) == "⭐⭐⭐⭐ Strong consensus"
        assert db.calculate_confidence(0.99) == "⭐⭐⭐⭐ Strong consensus"

    def test_majority(self, db):
        assert db.calculate_confidence(0.60) == "⭐⭐⭐ Majority consensus"
        assert db.calculate_confidence(0.74) == "⭐⭐⭐ Majority consensus"

    def test_weak(self, db):
        assert db.calculate_confidence(0.40) == "⭐⭐ Weak consensus"
        assert db.calculate_confidence(0.59) == "⭐⭐ Weak consensus"

    def test_no_consensus(self, db):
        assert db.calculate_confidence(0.0) == "⭐ No consensus"
        assert db.calculate_confidence(0.39) == "⭐ No consensus"


# ---------------------------------------------------------------------------
# estimate_local_inference_cost
# ---------------------------------------------------------------------------


class TestEstimateLocalInferenceCost:
    def test_zero_duration(self, db):
        cost = db.estimate_local_inference_cost(0)
        assert cost == 0.0

    def test_positive_cost_for_positive_duration(self, db):
        cost = db.estimate_local_inference_cost(3600)
        assert cost > 0

    def test_cost_scales_with_duration(self, db):
        cost_1h = db.estimate_local_inference_cost(3600)
        cost_2h = db.estimate_local_inference_cost(7200)
        assert abs(cost_2h - 2 * cost_1h) < 1e-9

    def test_known_value(self, db):
        # 1 hour: energy = (300 * 3600) / (1000 * 3600) * 0.14 = 0.3 kWh * 0.14 = 0.042
        # depreciation = (2000 / (4 * 2000 * 3600)) * 3600 = 2000/28800000 * 3600 = 0.25
        cost = db.estimate_local_inference_cost(3600)
        assert abs(cost - 0.292) < 0.001


# ---------------------------------------------------------------------------
# _validate_answer
# ---------------------------------------------------------------------------


class TestValidateAnswer:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_exact_match_first_choice(self, db):
        result = self._run(db._validate_answer("A", ["A", "B", "C"]))
        assert result == "A"

    def test_case_insensitive(self, db):
        result = self._run(db._validate_answer("b", ["A", "B", "C"]))
        assert result == "B"

    def test_prefix_match(self, db):
        result = self._run(db._validate_answer("B. Paris", ["A", "B", "C"]))
        assert result == "B"

    def test_suffix_match(self, db):
        result = self._run(db._validate_answer("the answer is A", ["A", "B", "C"]))
        assert result == "A"

    def test_double_quoted_answer(self, db):
        result = self._run(db._validate_answer('"A"', ["A", "B", "C"]))
        assert result == "A"

    def test_single_quoted_answer(self, db):
        result = self._run(db._validate_answer("'B'", ["A", "B", "C"]))
        assert result == "B"

    def test_think_tag_stripped(self, db):
        raw = "<think>Let me think...</think>\n\nA"
        result = self._run(db._validate_answer(raw, ["A", "B", "C"]))
        assert result == "A"

    def test_no_match_returns_empty(self, db):
        result = self._run(db._validate_answer("zzz", ["A", "B", "C"]))
        assert result == ""

    def test_fuzzy_match_close_string(self, db):
        choices = ["Paris", "London", "Berlin"]
        # "Pariz" should fuzzy-match to "Paris"
        result = self._run(db._validate_answer("Pariz", choices))
        assert result == "Paris"

    def test_fuzzy_no_match_below_cutoff(self, db):
        choices = ["Alpha", "Beta", "Gamma"]
        result = self._run(db._validate_answer("xyz", choices))
        assert result == ""

    def test_multiple_choices_selects_best(self, db):
        choices = ["strongly agree", "agree", "disagree", "strongly disagree"]
        result = self._run(db._validate_answer("strongly agree", choices))
        assert result == "strongly agree"

    def test_triple_backtick_wrapped(self, db):
        result = self._run(db._validate_answer("```\nFalse\n```", ["False", "True"]))
        assert result == "False"

    def test_triple_backtick_inline(self, db):
        result = self._run(db._validate_answer("```False```", ["False", "True"]))
        assert result == "False"

    def test_triple_backtick_with_lang_tag(self, db):
        result = self._run(db._validate_answer("```text\nTrue\n```", ["False", "True"]))
        assert result == "True"

    def test_triple_backtick_with_reasoning(self, db):
        raw = "<think>The answer is clearly False.</think>\n```\nFalse\n```"
        result = self._run(db._validate_answer(raw, ["False", "True"]))
        assert result == "False"

    def test_list_wrapped_answer(self, db):
        raw = "<think>thinking...</think>\n```text\n['no']\n```"
        result = self._run(db._validate_answer(raw, ["yes", "no"]))
        assert result == "no"

    def test_list_wrapped_answer_no_backticks(self, db):
        result = self._run(db._validate_answer("['False']", ["False", "True"]))
        assert result == "False"

    def test_substring_ambiguous_no_match(self, db):
        result = self._run(db._validate_answer("['agree']", ["agree", "disagree"]))
        assert result == "agree"

    def test_numeric_10_not_matched_as_0(self, db):
        choices = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]
        result = self._run(db._validate_answer("10", choices))
        assert result == "10"

    def test_numeric_10_with_reasoning(self, db):
        choices = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]
        raw = "<think>This resume is excellent.</think>\n10"
        result = self._run(db._validate_answer(raw, choices))
        assert result == "10"

    def test_longer_choice_preferred_over_substring(self, db):
        choices = ["agree", "strongly agree"]
        result = self._run(db._validate_answer("strongly agree", choices))
        assert result == "strongly agree"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_mock_pool(conn):
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


# ---------------------------------------------------------------------------
# connect_db — onboarding breakglass (credential reset on existing user)
# ---------------------------------------------------------------------------


class TestOnboardingBreakglass:
    """Regression: _grpc_create catches UniqueViolationError internally and
    returns a StatusReply with ERROR code — it never re-raises.  The
    onboarding code must check the return code and fall through to the
    update path so an existing admin's credentials are actually reset."""

    def _make_stub(self, existing_pb):
        updated_protos = []

        class _StubDBOnboard(DBMixin):
            db_pool = None

            async def _grpc_create(self, id, proto_obj, obj_type, overwrite=False):
                reply = service_pb2.StatusReply()
                reply.code = service_pb2.ResponseCode.ERROR
                reply.reason = "Object already exists"
                return reply

            async def _grpc_update(self, id, proto_obj, obj_type, obj_owner=None):
                updated_protos.append(proto_obj)
                reply = service_pb2.StatusReply()
                reply.code = service_pb2.ResponseCode.SUCCESS
                return reply

        conn = mock.MagicMock()
        conn.execute = mock.AsyncMock()
        conn.fetchrow = mock.AsyncMock(return_value=(existing_pb.SerializeToString(),))
        conn.fetchval = mock.AsyncMock(return_value=False)
        txn = mock.MagicMock()
        txn.__aenter__ = mock.AsyncMock(return_value=None)
        txn.__aexit__ = mock.AsyncMock(return_value=False)
        conn.transaction.return_value = txn

        stub = _StubDBOnboard()
        stub.db_pool = _make_mock_pool(conn)
        return stub, updated_protos

    def test_existing_user_gets_password_reset(self):
        existing_pb = service_pb2.User()
        existing_pb.id = "admin@example.com"
        existing_pb.role = service_pb2.UserRole.NORMAL
        existing_pb.password_hash = ""

        stub, updated = self._make_stub(existing_pb)
        _run(
            stub.connect_db(
                admin_onboarding_id="admin@example.com",
                admin_onboarding_password="new-breakglass-pw",
            )
        )

        assert len(updated) == 1
        assert updated[0].role == service_pb2.UserRole.ADMIN
        assert check_password_hash(updated[0].password_hash, "new-breakglass-pw")

    def test_existing_user_gets_api_key_reset(self):
        existing_pb = service_pb2.User()
        existing_pb.id = "admin@example.com"
        existing_pb.api_key.hash = ""

        stub, updated = self._make_stub(existing_pb)
        _run(
            stub.connect_db(
                admin_onboarding_id="admin@example.com",
                admin_onboarding_api_key="new-api-key",
            )
        )

        assert len(updated) == 1
        assert updated[0].api_key.hash != ""
        assert updated[0].api_key.active is True

    def test_new_user_does_not_trigger_update(self):
        """When _grpc_create succeeds, the update path must not run."""

        class _StubDBCreate(DBMixin):
            db_pool = None
            update_called = False

            async def _grpc_create(self, id, proto_obj, obj_type, overwrite=False):
                reply = service_pb2.StatusReply()
                reply.code = service_pb2.ResponseCode.SUCCESS
                return reply

            async def _grpc_update(self, id, proto_obj, obj_type, obj_owner=None):
                self.update_called = True

        conn = mock.MagicMock()
        conn.execute = mock.AsyncMock()
        conn.fetchval = mock.AsyncMock(return_value=False)
        txn = mock.MagicMock()
        txn.__aenter__ = mock.AsyncMock(return_value=None)
        txn.__aexit__ = mock.AsyncMock(return_value=False)
        conn.transaction.return_value = txn

        stub = _StubDBCreate()
        stub.db_pool = _make_mock_pool(conn)
        _run(
            stub.connect_db(
                admin_onboarding_id="admin@example.com",
                admin_onboarding_password="pw",
            )
        )

        assert not stub.update_called


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
