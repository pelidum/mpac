"""RateLimitsMixin: enforce per-user token-rate and spend limits.

Limits are stored on User.rate_limits (UserRateLimits proto):
  tokens_per_minute  — max tokens in a 60-second sliding window. 0 = unlimited.
  daily_spend        — max cumulative spend (UTC calendar day), in USD cents. 0 = unlimited.
  monthly_spend      — max cumulative spend (UTC calendar month), in USD cents. 0 = unlimited.

Spend is aggregated from test_runs.total_cost_usd (float USD) and compared
against the limit in integer cents to avoid float rounding surprises.

Token-rate tracking uses the user_tpm_windows table (created by DBMixin.connect_db),
which stores (user_id, window_minute, tokens_used) keyed on the Unix epoch minute.
Upserts are atomic; stale windows are lazily deleted on each write.
"""

import time
import traceback

import grpc

from absl import logging
from cachetools import TTLCache

from server import service_pb2

_DEBUG_RANDOM_FLAT_COST = 0.001
_ESTIMATED_INPUT_TOKENS = 600
_ESTIMATED_OUTPUT_TOKENS = 100
_ESTIMATED_REASONING_INPUT_TOKENS = 700
_ESTIMATED_REASONING_OUTPUT_TOKENS = 300


def _estimated_item_cost(
    model_pb: service_pb2.Model,
    include_reasoning: bool,
) -> float:
    """Estimated USD cost for one model answering one item.

    Returns 0.0 when the model has no pricing configured.
    """
    p = model_pb.pricing
    if p.input_token_cost <= 0 and p.output_token_cost <= 0:
        return 0.0
    cost = (
        _ESTIMATED_INPUT_TOKENS * p.input_token_cost
        + _ESTIMATED_OUTPUT_TOKENS * p.output_token_cost
    )
    if include_reasoning:
        cost += (
            _ESTIMATED_REASONING_INPUT_TOKENS * p.input_token_cost
            + _ESTIMATED_REASONING_OUTPUT_TOKENS * p.output_token_cost
        )
    return cost


class RateLimitsMixin:
    _rate_limits_cache: TTLCache = TTLCache(maxsize=500, ttl=30)

    async def _get_user_rate_limits(self, user_id: str) -> service_pb2.UserRateLimits:
        """Return cached UserRateLimits for user_id, fetching from DB on cache miss."""
        if user_id in self._rate_limits_cache:
            return self._rate_limits_cache[user_id]
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT proto_bytes FROM users WHERE proto_jsonb ->> 'id' = $1 LIMIT 1;",
                    user_id,
                )
            if row and row[0]:
                user_pb = service_pb2.User()
                user_pb.ParseFromString(row[0])
                limits = user_pb.rate_limits
                self._rate_limits_cache[user_id] = limits
                return limits
        except Exception:
            logging.error(traceback.format_exc())
            raise
        return service_pb2.UserRateLimits()

    def _invalidate_rate_limits_cache(self, user_id: str) -> None:
        """Evict a user's cached limits so the next call fetches fresh data."""
        self._rate_limits_cache.pop(user_id, None)

    async def _get_current_spend(self, user_id: str) -> tuple[float, float]:
        """Return (daily_usd, monthly_usd) spend for user_id.

        Raises on DB error so callers fail closed rather than assuming zero spend.
        """
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH daily AS (
                    SELECT COALESCE(
                        SUM((proto_jsonb->>'total_cost_usd')::float), 0.0
                    ) AS total
                    FROM test_runs
                    WHERE proto_jsonb->>'owner' = $1
                      AND created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                ),
                monthly AS (
                    SELECT COALESCE(
                        SUM((proto_jsonb->>'total_cost_usd')::float), 0.0
                    ) AS total
                    FROM test_runs
                    WHERE proto_jsonb->>'owner' = $1
                      AND created_at >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
                )
                SELECT daily.total AS daily_total, monthly.total AS monthly_total
                FROM daily, monthly
                """,
                user_id,
            )
        if row:
            return float(row["daily_total"]), float(row["monthly_total"])
        return 0.0, 0.0

    async def check_spend_limits(
        self,
        user_id: str,
        context: grpc.aio.ServicerContext,
    ) -> None:
        """Abort with RESOURCE_EXHAUSTED if the user has exceeded daily or monthly spend.

        Skips all DB work when both limits are 0 (unlimited).
        """
        limits = await self._get_user_rate_limits(user_id)
        if limits.daily_spend == 0 and limits.monthly_spend == 0:
            return

        daily_usd, monthly_usd = await self._get_current_spend(user_id)

        if limits.daily_spend > 0 and int(daily_usd * 100) >= limits.daily_spend:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"Daily spend limit exceeded "
                f"(${daily_usd:.4f} of ${limits.daily_spend / 100:.2f}).",
            )

        if limits.monthly_spend > 0 and int(monthly_usd * 100) >= limits.monthly_spend:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"Monthly spend limit exceeded "
                f"(${monthly_usd:.4f} of ${limits.monthly_spend / 100:.2f}).",
            )

    async def _projected_run_cost(
        self,
        request: service_pb2.TestRunRequest,
        backend: service_pb2.Backend,
    ) -> float | None:
        """Estimate the total cost of a run before inference starts.

        Returns None when cost cannot be determined — callers should skip the
        pre-flight check and rely on mid-run tracking instead.

        DEBUG_RANDOM returns an exact value. Priced backends return a
        conservative per-answer estimate; runs with include_reasoning are
        projected with the second reasoning call included, since that pass
        roughly triples the per-item cost. If no model has pricing, returns
        None.
        """
        num_models = len(request.models)
        if num_models == 0:
            return None

        num_items = request.sample_size if request.sample_size > 0 else 10

        if backend.backend_type == service_pb2.BackendType.DEBUG_RANDOM:
            return _DEBUG_RANDOM_FLAT_COST

        total = 0.0
        priced = 0
        for model_pb in request.models:
            cost = _estimated_item_cost(model_pb, request.include_reasoning)
            if cost > 0:
                total += num_items * cost
                priced += 1

        return total if priced > 0 else None

    async def _projected_benchmark_cost(
        self,
        models,
        sample_size: int,
        include_reasoning: bool = False,
    ) -> float | None:
        """Estimate the total cost of a single-elimination benchmark.

        A bracket of n models always plays n-1 matches (each match eliminates
        exactly one model), i.e. 2*(n-1) match slots across the field. The
        bracket isn't known at creation time, so each model is assumed to fill
        its average share of slots, 2*(n-1)/n. Returns None when no model has
        pricing or n < 2.
        """
        n = len(models)
        if n < 2:
            return None

        num_items = sample_size if sample_size > 0 else 25

        total_item_cost = 0.0
        priced = 0
        for model_pb in models:
            cost = _estimated_item_cost(model_pb, include_reasoning)
            if cost > 0:
                total_item_cost += cost
                priced += 1
        if priced == 0:
            return None

        return (2 * (n - 1) / n) * num_items * total_item_cost

    async def check_projected_cost(
        self,
        user_id: str,
        projected: float | None,
        context: grpc.aio.ServicerContext,
        label: str = "run",
    ) -> None:
        """Abort with RESOURCE_EXHAUSTED if projected cost exceeds remaining budget.

        No-op when projected is None (cost unknown — mid-run tracking is the
        fallback) or when the user has no spend limits configured.
        """
        if projected is None:
            return
        limits = await self._get_user_rate_limits(user_id)
        if limits.daily_spend == 0 and limits.monthly_spend == 0:
            return

        daily_usd, monthly_usd = await self._get_current_spend(user_id)

        if limits.daily_spend > 0:
            remaining = (limits.daily_spend / 100) - daily_usd
            if projected > remaining:
                await context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"Projected {label} cost ${projected:.2f} exceeds remaining "
                    f"daily budget ${remaining:.2f}.",
                )

        if limits.monthly_spend > 0:
            remaining = (limits.monthly_spend / 100) - monthly_usd
            if projected > remaining:
                await context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"Projected {label} cost ${projected:.2f} exceeds remaining "
                    f"monthly budget ${remaining:.2f}.",
                )

    async def record_and_check_tpm(self, user_id: str, tokens: int) -> bool:
        """Atomically record tokens for the current 60-second window.

        Returns True if the user is within their tokens_per_minute limit (or
        has no limit). Returns False when the window total exceeds the limit
        or on DB errors (fail closed).

        Stale windows older than 2 minutes are lazily cleaned up on each call.
        """
        limits = await self._get_user_rate_limits(user_id)
        if limits.tokens_per_minute == 0:
            return True

        window_minute = int(time.time()) // 60
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO user_tpm_windows (user_id, window_minute, tokens_used)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, window_minute)
                    DO UPDATE SET
                        tokens_used = user_tpm_windows.tokens_used + EXCLUDED.tokens_used
                    RETURNING tokens_used
                    """,
                    user_id,
                    window_minute,
                    tokens,
                )
                await conn.execute(
                    "DELETE FROM user_tpm_windows WHERE window_minute < $1",
                    window_minute - 2,
                )
            if row:
                return row["tokens_used"] <= limits.tokens_per_minute
        except Exception:
            logging.error(traceback.format_exc())
            return False
