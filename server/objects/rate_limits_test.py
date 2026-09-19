"""Unit tests for RateLimitsMixin: cache behaviour, spend-limit enforcement, TPM tracking."""

import asyncio
import unittest.mock as mock

import grpc
import pytest

from server import service_pb2
from server.objects.rate_limits import (
    _DEBUG_RANDOM_FLAT_COST,
    RateLimitsMixin,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Aborted(BaseException):
    """Raised by the mock context.abort so test code propagates cleanly."""

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


def _make_limits(
    tokens_per_minute: int = 0,
    daily_spend: int = 0,
    monthly_spend: int = 0,
) -> service_pb2.UserRateLimits:
    limits = service_pb2.UserRateLimits()
    limits.tokens_per_minute = tokens_per_minute
    limits.daily_spend = daily_spend
    limits.monthly_spend = monthly_spend
    return limits


def _make_user_bytes(user_id: str, limits: service_pb2.UserRateLimits) -> bytes:
    user = service_pb2.User()
    user.id = user_id
    user.rate_limits.CopyFrom(limits)
    return user.SerializeToString()


def _make_pool(fetchrow_return=None, execute_return=None):
    """Return a mock asyncpg pool that returns the given value from fetchrow."""
    conn = mock.AsyncMock()
    conn.fetchrow = mock.AsyncMock(return_value=fetchrow_return)
    conn.execute = mock.AsyncMock(return_value=execute_return)
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool, conn


class _StubRL(RateLimitsMixin):
    """Minimal concrete stub for RateLimitsMixin (requires db_pool attribute)."""

    def __init__(self, db_pool=None):
        self.db_pool = db_pool
        # Reset class-level cache for each test instance
        RateLimitsMixin._rate_limits_cache.clear()


# ---------------------------------------------------------------------------
# _get_user_rate_limits
# ---------------------------------------------------------------------------


class TestGetUserRateLimits:
    def test_returns_limits_from_db(self):
        limits = _make_limits(tokens_per_minute=100, daily_spend=500)
        user_bytes = _make_user_bytes("alice@example.com", limits)
        row = {"proto_bytes": user_bytes}
        # asyncpg rows are accessed by key — mock as a dict-like
        row_mock = mock.MagicMock()
        row_mock.__bool__ = mock.MagicMock(return_value=True)
        row_mock.__getitem__ = mock.MagicMock(side_effect=lambda k: user_bytes)

        pool, conn = _make_pool(fetchrow_return=row_mock)
        stub = _StubRL(db_pool=pool)

        result = _run(stub._get_user_rate_limits("alice@example.com"))
        assert result.tokens_per_minute == 100
        assert result.daily_spend == 500

    def test_caches_result_on_second_call(self):
        limits = _make_limits(daily_spend=200)
        user_bytes = _make_user_bytes("bob@example.com", limits)
        row_mock = mock.MagicMock()
        row_mock.__bool__ = mock.MagicMock(return_value=True)
        row_mock.__getitem__ = mock.MagicMock(side_effect=lambda k: user_bytes)

        pool, conn = _make_pool(fetchrow_return=row_mock)
        stub = _StubRL(db_pool=pool)

        _run(stub._get_user_rate_limits("bob@example.com"))
        _run(stub._get_user_rate_limits("bob@example.com"))

        # DB should only be hit once
        assert conn.fetchrow.call_count == 1

    def test_returns_empty_limits_when_user_not_found(self):
        row_mock = mock.MagicMock()
        row_mock.__bool__ = mock.MagicMock(return_value=False)

        pool, conn = _make_pool(fetchrow_return=row_mock)
        stub = _StubRL(db_pool=pool)

        result = _run(stub._get_user_rate_limits("ghost@example.com"))
        assert result.tokens_per_minute == 0
        assert result.daily_spend == 0
        assert result.monthly_spend == 0

    def test_raises_on_db_error(self):
        pool = mock.MagicMock()
        pool.acquire.side_effect = RuntimeError("connection refused")
        stub = _StubRL(db_pool=pool)

        with pytest.raises(RuntimeError, match="connection refused"):
            _run(stub._get_user_rate_limits("user@example.com"))

    def test_invalidate_cache_forces_refetch(self):
        limits = _make_limits(daily_spend=999)
        user_bytes = _make_user_bytes("carol@example.com", limits)
        row_mock = mock.MagicMock()
        row_mock.__bool__ = mock.MagicMock(return_value=True)
        row_mock.__getitem__ = mock.MagicMock(side_effect=lambda k: user_bytes)

        pool, conn = _make_pool(fetchrow_return=row_mock)
        stub = _StubRL(db_pool=pool)

        _run(stub._get_user_rate_limits("carol@example.com"))
        stub._invalidate_rate_limits_cache("carol@example.com")
        _run(stub._get_user_rate_limits("carol@example.com"))

        assert conn.fetchrow.call_count == 2


# ---------------------------------------------------------------------------
# check_spend_limits — no-op when unlimited
# ---------------------------------------------------------------------------


class TestCheckSpendLimitsNoOp:
    def test_skips_db_when_both_limits_zero(self):
        """When daily_spend=0 and monthly_spend=0, no DB query should run."""
        limits = _make_limits()  # all 0
        user_bytes = _make_user_bytes("nolimit@example.com", limits)
        row_mock = mock.MagicMock()
        row_mock.__bool__ = mock.MagicMock(return_value=True)
        row_mock.__getitem__ = mock.MagicMock(side_effect=lambda k: user_bytes)

        pool, conn = _make_pool(fetchrow_return=row_mock)
        stub = _StubRL(db_pool=pool)

        # Prime the cache with no-limit settings
        _run(stub._get_user_rate_limits("nolimit@example.com"))
        conn.fetchrow.reset_mock()

        _run(stub.check_spend_limits("nolimit@example.com", _make_context()))
        # The spend aggregation query must NOT have been called
        assert conn.fetchrow.call_count == 0

    def test_does_not_abort_when_under_limits(self):
        limits = _make_limits(daily_spend=10000, monthly_spend=50000)

        # Return a row where spend is well under the limits
        agg_row = {"daily_total": 0.50, "monthly_total": 1.00}
        agg_mock = mock.MagicMock()
        agg_mock.__bool__ = mock.MagicMock(return_value=True)
        agg_mock.__getitem__ = mock.MagicMock(side_effect=lambda k: agg_row[k])

        pool, conn = _make_pool(fetchrow_return=agg_mock)
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["dave@example.com"] = limits

        # Should complete without raising _Aborted
        _run(stub.check_spend_limits("dave@example.com", _make_context()))


# ---------------------------------------------------------------------------
# check_spend_limits — daily limit
# ---------------------------------------------------------------------------


class TestCheckSpendLimitsDaily:
    def _setup(self, daily_total: float, daily_limit_cents: int):
        limits = _make_limits(daily_spend=daily_limit_cents)

        agg_row = {"daily_total": daily_total, "monthly_total": 0.0}
        agg_mock = mock.MagicMock()
        agg_mock.__bool__ = mock.MagicMock(return_value=True)
        agg_mock.__getitem__ = mock.MagicMock(side_effect=lambda k: agg_row[k])

        pool, _ = _make_pool(fetchrow_return=agg_mock)
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits
        return stub

    def test_aborts_when_daily_limit_exceeded(self):
        # $5.00 spent, $4.99 limit (499 cents)
        stub = self._setup(daily_total=5.00, daily_limit_cents=499)
        with pytest.raises(_Aborted) as exc_info:
            _run(stub.check_spend_limits("user@example.com", _make_context()))
        assert exc_info.value.code == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert "Daily" in exc_info.value.message

    def test_aborts_exactly_at_daily_limit(self):
        # $5.00 spent, exactly $5.00 limit (500 cents) — should abort (>=)
        stub = self._setup(daily_total=5.00, daily_limit_cents=500)
        with pytest.raises(_Aborted) as exc_info:
            _run(stub.check_spend_limits("user@example.com", _make_context()))
        assert exc_info.value.code == grpc.StatusCode.RESOURCE_EXHAUSTED

    def test_does_not_abort_one_cent_under_daily_limit(self):
        # $4.99 spent, $5.00 limit (500 cents) — should not abort
        stub = self._setup(daily_total=4.99, daily_limit_cents=500)
        _run(stub.check_spend_limits("user@example.com", _make_context()))

    def test_ignores_daily_when_zero(self):
        # daily_spend=0 means unlimited — even with $999 spent, no abort
        limits = _make_limits(daily_spend=0, monthly_spend=50000)

        agg_row = {"daily_total": 999.0, "monthly_total": 0.0}
        agg_mock = mock.MagicMock()
        agg_mock.__bool__ = mock.MagicMock(return_value=True)
        agg_mock.__getitem__ = mock.MagicMock(side_effect=lambda k: agg_row[k])

        pool, _ = _make_pool(fetchrow_return=agg_mock)
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits
        _run(stub.check_spend_limits("user@example.com", _make_context()))


# ---------------------------------------------------------------------------
# check_spend_limits — monthly limit
# ---------------------------------------------------------------------------


class TestCheckSpendLimitsMonthly:
    def _setup(self, monthly_total: float, monthly_limit_cents: int):
        limits = _make_limits(monthly_spend=monthly_limit_cents)

        agg_row = {"daily_total": 0.0, "monthly_total": monthly_total}
        agg_mock = mock.MagicMock()
        agg_mock.__bool__ = mock.MagicMock(return_value=True)
        agg_mock.__getitem__ = mock.MagicMock(side_effect=lambda k: agg_row[k])

        pool, _ = _make_pool(fetchrow_return=agg_mock)
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits
        return stub

    def test_aborts_when_monthly_limit_exceeded(self):
        # $50.01 spent, $50.00 limit (5000 cents)
        stub = self._setup(monthly_total=50.01, monthly_limit_cents=5000)
        with pytest.raises(_Aborted) as exc_info:
            _run(stub.check_spend_limits("user@example.com", _make_context()))
        assert exc_info.value.code == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert "Monthly" in exc_info.value.message

    def test_does_not_abort_under_monthly_limit(self):
        stub = self._setup(monthly_total=49.99, monthly_limit_cents=5000)
        _run(stub.check_spend_limits("user@example.com", _make_context()))

    def test_ignores_monthly_when_zero(self):
        limits = _make_limits(daily_spend=10000, monthly_spend=0)

        agg_row = {"daily_total": 0.0, "monthly_total": 999.0}
        agg_mock = mock.MagicMock()
        agg_mock.__bool__ = mock.MagicMock(return_value=True)
        agg_mock.__getitem__ = mock.MagicMock(side_effect=lambda k: agg_row[k])

        pool, _ = _make_pool(fetchrow_return=agg_mock)
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits
        _run(stub.check_spend_limits("user@example.com", _make_context()))


# ---------------------------------------------------------------------------
# check_spend_limits — error handling
# ---------------------------------------------------------------------------


class TestCheckSpendLimitsErrors:
    def test_raises_on_db_error(self):
        """DB failure during spend aggregation must propagate (fail closed)."""
        limits = _make_limits(daily_spend=100)

        pool = mock.MagicMock()
        pool.acquire.side_effect = RuntimeError("db down")
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits

        with pytest.raises(RuntimeError, match="db down"):
            _run(stub.check_spend_limits("user@example.com", _make_context()))


# ---------------------------------------------------------------------------
# record_and_check_tpm
# ---------------------------------------------------------------------------


class TestRecordAndCheckTpm:
    def test_returns_true_when_no_limit(self):
        """tokens_per_minute=0 means unlimited — no DB call needed."""
        limits = _make_limits(tokens_per_minute=0)

        pool, conn = _make_pool()
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits

        result = _run(stub.record_and_check_tpm("user@example.com", 9999))
        assert result is True
        assert conn.fetchrow.call_count == 0

    def test_returns_true_when_within_limit(self):
        limits = _make_limits(tokens_per_minute=1000)

        tpm_row = mock.MagicMock()
        tpm_row.__bool__ = mock.MagicMock(return_value=True)
        tpm_row.__getitem__ = mock.MagicMock(
            side_effect=lambda k: 500
        )  # 500 tokens used

        pool, conn = _make_pool(fetchrow_return=tpm_row)
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits

        result = _run(stub.record_and_check_tpm("user@example.com", 100))
        assert result is True

    def test_returns_false_when_over_limit(self):
        limits = _make_limits(tokens_per_minute=1000)

        tpm_row = mock.MagicMock()
        tpm_row.__bool__ = mock.MagicMock(return_value=True)
        tpm_row.__getitem__ = mock.MagicMock(side_effect=lambda k: 1001)  # over limit

        pool, conn = _make_pool(fetchrow_return=tpm_row)
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits

        result = _run(stub.record_and_check_tpm("user@example.com", 100))
        assert result is False

    def test_returns_true_at_exactly_the_limit(self):
        """Tokens equal to the limit are allowed (<= not <)."""
        limits = _make_limits(tokens_per_minute=500)

        tpm_row = mock.MagicMock()
        tpm_row.__bool__ = mock.MagicMock(return_value=True)
        tpm_row.__getitem__ = mock.MagicMock(side_effect=lambda k: 500)

        pool, _ = _make_pool(fetchrow_return=tpm_row)
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits

        result = _run(stub.record_and_check_tpm("user@example.com", 50))
        assert result is True

    def test_fails_closed_on_db_error(self):
        """DB errors must throttle inference — return False."""
        limits = _make_limits(tokens_per_minute=100)

        pool = mock.MagicMock()
        pool.acquire.side_effect = RuntimeError("db down")
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits

        result = _run(stub.record_and_check_tpm("user@example.com", 50))
        assert result is False

    def test_calls_delete_for_stale_windows(self):
        """After the upsert, old windows should be lazily cleaned up."""
        limits = _make_limits(tokens_per_minute=1000)

        tpm_row = mock.MagicMock()
        tpm_row.__bool__ = mock.MagicMock(return_value=True)
        tpm_row.__getitem__ = mock.MagicMock(side_effect=lambda k: 100)

        pool, conn = _make_pool(fetchrow_return=tpm_row)
        stub = _StubRL(db_pool=pool)
        RateLimitsMixin._rate_limits_cache["user@example.com"] = limits

        _run(stub.record_and_check_tpm("user@example.com", 100))
        assert conn.execute.called
        call_args = conn.execute.call_args[0]
        assert "DELETE FROM user_tpm_windows" in call_args[0]


# ---------------------------------------------------------------------------
# _get_current_spend
# ---------------------------------------------------------------------------


def _make_spend_row(daily: float, monthly: float):
    row = mock.MagicMock()
    row.__bool__ = mock.MagicMock(return_value=True)
    data = {"daily_total": daily, "monthly_total": monthly}
    row.__getitem__ = mock.MagicMock(side_effect=lambda k: data[k])
    return row


class TestGetCurrentSpend:
    def test_returns_daily_and_monthly(self):
        row = _make_spend_row(1.50, 12.00)
        pool, _ = _make_pool(fetchrow_return=row)
        stub = _StubRL(db_pool=pool)

        daily, monthly = _run(stub._get_current_spend("user@example.com"))
        assert abs(daily - 1.50) < 1e-9
        assert abs(monthly - 12.00) < 1e-9

    def test_returns_zeros_when_no_row(self):
        pool, _ = _make_pool(fetchrow_return=None)
        stub = _StubRL(db_pool=pool)

        daily, monthly = _run(stub._get_current_spend("user@example.com"))
        assert daily == 0.0
        assert monthly == 0.0

    def test_raises_on_db_error(self):
        pool = mock.MagicMock()
        pool.acquire.side_effect = RuntimeError("db down")
        stub = _StubRL(db_pool=pool)

        with pytest.raises(RuntimeError, match="db down"):
            _run(stub._get_current_spend("user@example.com"))


# ---------------------------------------------------------------------------
# _projected_run_cost
# ---------------------------------------------------------------------------


def _make_backend(backend_type: int) -> service_pb2.Backend:
    b = service_pb2.Backend()
    b.backend_type = backend_type
    return b


def _make_request(
    sample_size: int = 10,
    models: list | None = None,
) -> service_pb2.TestRunRequest:
    req = service_pb2.TestRunRequest()
    req.sample_size = sample_size
    if models:
        req.models.extend(models)
    return req


def _make_model_with_pricing(
    model_id: str = "m1",
    input_cost: float = 0.0,
    output_cost: float = 0.0,
) -> service_pb2.Model:
    m = service_pb2.Model()
    m.id = model_id
    m.pricing.input_token_cost = input_cost
    m.pricing.output_token_cost = output_cost
    return m


class TestProjectedRunCost:
    def test_debug_random_flat_cost(self):
        # DEBUG_RANDOM runs are recorded at a flat cost regardless of item or
        # model count (matches the accounting in runs.py).
        backend = _make_backend(service_pb2.BackendType.DEBUG_RANDOM)
        req = _make_request(sample_size=10, models=[_make_model_with_pricing()])
        stub = _StubRL()

        result = _run(stub._projected_run_cost(req, backend))
        assert abs(result - _DEBUG_RANDOM_FLAT_COST) < 1e-9

    def test_debug_random_flat_cost_independent_of_size(self):
        backend = _make_backend(service_pb2.BackendType.DEBUG_RANDOM)
        models = [_make_model_with_pricing("m1"), _make_model_with_pricing("m2")]
        req = _make_request(sample_size=400, models=models)
        stub = _StubRL()

        result = _run(stub._projected_run_cost(req, backend))
        assert abs(result - _DEBUG_RANDOM_FLAT_COST) < 1e-9

    def test_returns_none_when_no_models(self):
        backend = _make_backend(service_pb2.BackendType.DEBUG_RANDOM)
        req = _make_request(sample_size=10, models=[])
        stub = _StubRL()

        result = _run(stub._projected_run_cost(req, backend))
        assert result is None

    def test_priced_backend_uses_token_estimates(self):
        backend = _make_backend(service_pb2.BackendType.OPENAI)
        # $0.001 per input token, $0.002 per output token
        model = _make_model_with_pricing(input_cost=0.001, output_cost=0.002)
        req = _make_request(sample_size=5, models=[model])
        stub = _StubRL()

        result = _run(stub._projected_run_cost(req, backend))
        # cost = 600 * 0.001 + 100 * 0.002 = 0.60 + 0.20 = 0.80
        # total = 5 items * 0.80 = 4.00
        assert result == pytest.approx(4.00, rel=1e-5)

    def test_priced_backend_sums_multiple_models(self):
        backend = _make_backend(service_pb2.BackendType.OPENAI)
        m1 = _make_model_with_pricing("m1", input_cost=0.001, output_cost=0.002)
        m2 = _make_model_with_pricing("m2", input_cost=0.002, output_cost=0.004)
        req = _make_request(sample_size=2, models=[m1, m2])
        stub = _StubRL()

        result = _run(stub._projected_run_cost(req, backend))
        # m1: 2 * (600*0.001 + 100*0.002) = 2 * 0.80 = 1.60
        # m2: 2 * (600*0.002 + 100*0.004) = 2 * 1.60 = 3.20
        # total = 4.80
        assert result == pytest.approx(4.80, rel=1e-5)

    def test_returns_none_when_no_models_have_pricing(self):
        backend = _make_backend(service_pb2.BackendType.OPENAI)
        model = _make_model_with_pricing(input_cost=0.0, output_cost=0.0)
        req = _make_request(sample_size=10, models=[model])
        stub = _StubRL()

        result = _run(stub._projected_run_cost(req, backend))
        assert result is None

    def test_reasoning_adds_second_call_estimate(self):
        backend = _make_backend(service_pb2.BackendType.OPENAI)
        model = _make_model_with_pricing(input_cost=0.001, output_cost=0.002)
        req = _make_request(sample_size=5, models=[model])
        req.include_reasoning = True
        stub = _StubRL()

        result = _run(stub._projected_run_cost(req, backend))
        # base:      600 * 0.001 + 100 * 0.002 = 0.80
        # reasoning: 700 * 0.001 + 300 * 0.002 = 1.30
        # total = 5 items * 2.10 = 10.50
        assert result == pytest.approx(10.50, rel=1e-5)


# ---------------------------------------------------------------------------
# _projected_benchmark_cost
# ---------------------------------------------------------------------------


class TestProjectedBenchmarkCost:
    def test_returns_none_for_fewer_than_two_models(self):
        stub = _StubRL()
        models = [_make_model_with_pricing(input_cost=0.001)]

        result = _run(stub._projected_benchmark_cost(models, sample_size=10))
        assert result is None

    def test_returns_none_when_no_models_have_pricing(self):
        stub = _StubRL()
        models = [_make_model_with_pricing("m1"), _make_model_with_pricing("m2")]

        result = _run(stub._projected_benchmark_cost(models, sample_size=10))
        assert result is None

    def test_two_models_each_play_one_match(self):
        stub = _StubRL()
        m1 = _make_model_with_pricing("m1", input_cost=0.001, output_cost=0.002)
        m2 = _make_model_with_pricing("m2", input_cost=0.002, output_cost=0.004)

        result = _run(stub._projected_benchmark_cost([m1, m2], sample_size=10))
        # n=2 → 2*(n-1)/n = 1 match per model.
        # m1 item: 600*0.001 + 100*0.002 = 0.80; m2 item: 1.60
        # total = 1 * 10 * (0.80 + 1.60) = 24.0
        assert result == pytest.approx(24.0, rel=1e-5)

    def test_four_models_average_one_and_a_half_matches(self):
        stub = _StubRL()
        models = [
            _make_model_with_pricing(f"m{i}", input_cost=0.001, output_cost=0.002)
            for i in range(4)
        ]

        result = _run(stub._projected_benchmark_cost(models, sample_size=10))
        # n=4 → 2*(n-1)/n = 1.5 matches per model on average.
        # total = 1.5 * 10 * (4 * 0.80) = 48.0
        assert result == pytest.approx(48.0, rel=1e-5)

    def test_zero_sample_size_defaults_to_25(self):
        stub = _StubRL()
        m1 = _make_model_with_pricing("m1", input_cost=0.001, output_cost=0.002)
        m2 = _make_model_with_pricing("m2", input_cost=0.001, output_cost=0.002)

        result = _run(stub._projected_benchmark_cost([m1, m2], sample_size=0))
        # 1 match per model * 25 items * (0.80 + 0.80) = 40.0
        assert result == pytest.approx(40.0, rel=1e-5)

    def test_reasoning_increases_projection(self):
        stub = _StubRL()
        m1 = _make_model_with_pricing("m1", input_cost=0.001, output_cost=0.002)
        m2 = _make_model_with_pricing("m2", input_cost=0.001, output_cost=0.002)

        without = _run(stub._projected_benchmark_cost([m1, m2], sample_size=10))
        with_r = _run(
            stub._projected_benchmark_cost(
                [m1, m2], sample_size=10, include_reasoning=True
            )
        )
        # reasoning adds 700*0.001 + 300*0.002 = 1.30 per item per model
        # 1 match per model * 10 items * (1.30 + 1.30) = 26.0 extra
        assert with_r - without == pytest.approx(26.0, rel=1e-5)


# ---------------------------------------------------------------------------
# check_projected_cost
# ---------------------------------------------------------------------------


class TestCheckProjectedCost:
    def _stub_with(self, limits, daily=0.0, monthly=0.0):
        stub = _StubRL()
        stub._get_user_rate_limits = mock.AsyncMock(return_value=limits)
        stub._get_current_spend = mock.AsyncMock(return_value=(daily, monthly))
        return stub

    def test_noop_when_projected_is_none(self):
        stub = self._stub_with(_make_limits(daily_spend=100))
        ctx = _make_context()

        _run(stub.check_projected_cost("u@example.com", None, ctx))
        stub._get_user_rate_limits.assert_not_called()

    def test_noop_when_no_limits(self):
        stub = self._stub_with(_make_limits())
        ctx = _make_context()

        _run(stub.check_projected_cost("u@example.com", 1000.0, ctx))
        stub._get_current_spend.assert_not_called()

    def test_aborts_when_projected_exceeds_daily_remaining(self):
        # $2.00 daily limit, $1.50 already spent → $0.50 remaining
        stub = self._stub_with(_make_limits(daily_spend=200), daily=1.50)
        ctx = _make_context()

        with pytest.raises(_Aborted) as exc_info:
            _run(stub.check_projected_cost("u@example.com", 0.51, ctx))
        assert exc_info.value.code == grpc.StatusCode.RESOURCE_EXHAUSTED

    def test_passes_when_projected_within_daily_remaining(self):
        stub = self._stub_with(_make_limits(daily_spend=200), daily=1.50)
        ctx = _make_context()

        _run(stub.check_projected_cost("u@example.com", 0.49, ctx))

    def test_aborts_when_projected_exceeds_monthly_remaining(self):
        stub = self._stub_with(_make_limits(monthly_spend=1000), monthly=9.95)
        ctx = _make_context()

        with pytest.raises(_Aborted) as exc_info:
            _run(stub.check_projected_cost("u@example.com", 0.10, ctx))
        assert exc_info.value.code == grpc.StatusCode.RESOURCE_EXHAUSTED

    def test_label_appears_in_abort_message(self):
        stub = self._stub_with(_make_limits(daily_spend=100), daily=0.99)
        ctx = _make_context()

        with pytest.raises(_Aborted) as exc_info:
            _run(
                stub.check_projected_cost("u@example.com", 5.0, ctx, label="benchmark")
            )
        assert "benchmark" in exc_info.value.message


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
