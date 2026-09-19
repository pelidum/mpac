"""Unit tests for BenchmarksMixin: authentication guard, tournament logic, and resume."""

import asyncio
import unittest.mock as mock

import grpc
import pytest

from server import service_pb2
from server.objects.benchmarks import (
    BenchmarksMixin,
    _advisory_lock_key,
    _reconstruct_bracket,
)
from server.objects.rate_limits import RateLimitsMixin


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# BaseException avoids being swallowed by the broad `except Exception` handler
# inside CreateBenchmark.
class _Aborted(BaseException):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _make_context():
    ctx = mock.AsyncMock()
    ctx.cancelled = mock.MagicMock(return_value=False)

    async def abort(code, msg):
        raise _Aborted(code, msg)

    ctx.abort = abort
    return ctx


async def _drain(gen):
    results = []
    async for r in gen:
        results.append(r)
    return results


class _StubBenchmarks(BenchmarksMixin, RateLimitsMixin):
    _request_owner = "user@example.com"
    _test_type = service_pb2.EVALUATION
    db_pool = None

    async def _get_user_rate_limits(self, user_id):
        return service_pb2.UserRateLimits()

    async def _get_current_spend(self, user_id):
        return 0.0, 0.0

    async def get_request_owner(self, request):
        return self._request_owner

    async def GetTest(self, request, context):
        test_pb = service_pb2.Test()
        test_pb.id = request.id
        test_pb.type = self._test_type
        return test_pb

    async def CreateTestRun(self, request, context):
        reply = service_pb2.TestRunReply()
        reply.run_id = "run-1"
        reply.code = service_pb2.ResponseCode.IN_PROGRESS
        yield reply

    async def GetTestRun(self, request, context):
        run_pb = service_pb2.TestRun()
        run_pb.id = request.id
        run_pb.status = service_pb2.ResponseCode.SUCCESS
        return run_pb

    async def _grpc_create(self, id, proto_obj, obj_type, overwrite=False):
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

    async def _grpc_update(self, id, proto_obj, obj_type, obj_owner=None):
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)


def _make_benchmark_pb(models, test_id="eval-1", sample_size=25):
    bp = service_pb2.Benchmark()
    bp.id = "bench-1"
    bp.test_id = test_id
    bp.status = service_pb2.ResponseCode.IN_PROGRESS
    bp.models.extend(models)
    bp.sample_size = sample_size
    bp.model_count = len(models)
    return bp


# ---------------------------------------------------------------------------
# CreateBenchmark — auth and test-type guards
# ---------------------------------------------------------------------------


class TestCreateBenchmarkGuards:
    def test_no_owner_aborts_unauthenticated(self):
        stub = _StubBenchmarks()
        stub._request_owner = None
        request = service_pb2.BenchmarkRequest(test_id="test-1")

        with pytest.raises(_Aborted) as exc_info:
            _run(_drain(stub.CreateBenchmark(request=request, context=_make_context())))
        assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED

    def test_survey_type_aborts_invalid_argument(self):
        stub = _StubBenchmarks()
        stub._test_type = service_pb2.SURVEY  # type 2, requires EVALUATION (type 1)
        request = service_pb2.BenchmarkRequest(test_id="survey-1")

        with pytest.raises(_Aborted) as exc_info:
            _run(_drain(stub.CreateBenchmark(request=request, context=_make_context())))
        assert exc_info.value.code == grpc.StatusCode.INVALID_ARGUMENT

    def test_evaluation_type_yields_single_in_progress_response(self):
        stub = _StubBenchmarks()
        stub._test_type = service_pb2.EVALUATION

        with mock.patch("asyncio.create_task"):
            model = service_pb2.Model(id="p/m")
            request = service_pb2.BenchmarkRequest(test_id="eval-1", models=[model])
            results = _run(
                _drain(stub.CreateBenchmark(request=request, context=_make_context()))
            )

        assert len(results) == 1
        assert results[0].benchmark_id
        assert results[0].code == service_pb2.ResponseCode.IN_PROGRESS

    def test_models_persisted_on_benchmark(self):
        stub = _StubBenchmarks()
        created_protos = []
        original_create = stub._grpc_create

        async def capture_create(**kwargs):
            if kwargs.get("obj_type") == "benchmarks":
                created_protos.append(kwargs["proto_obj"])
            return await original_create(**kwargs)

        stub._grpc_create = capture_create

        with mock.patch("asyncio.create_task"):
            models = [service_pb2.Model(id="a/1"), service_pb2.Model(id="b/2")]
            request = service_pb2.BenchmarkRequest(
                test_id="eval-1", models=models, sample_size=10
            )
            _run(_drain(stub.CreateBenchmark(request=request, context=_make_context())))

        assert len(created_protos) == 1
        bp = created_protos[0]
        assert len(bp.models) == 2
        assert {m.id for m in bp.models} == {"a/1", "b/2"}
        assert bp.sample_size == 10


# ---------------------------------------------------------------------------
# _do_benchmark — tournament logic
# ---------------------------------------------------------------------------


class TestDoBenchmark:
    def test_single_model_wins_immediately(self):
        stub = _StubBenchmarks()
        models = [service_pb2.Model(id="only/model")]
        benchmark_pb = _make_benchmark_pb(models)

        _run(stub._do_benchmark(benchmark_pb))

        assert benchmark_pb.status == service_pb2.ResponseCode.SUCCESS
        assert benchmark_pb.winner == "only/model"
        assert len(benchmark_pb.match_history) == 0

    def test_two_models_produces_one_match_and_winner(self):
        stub = _StubBenchmarks()
        models = [service_pb2.Model(id="a/1"), service_pb2.Model(id="b/2")]
        benchmark_pb = _make_benchmark_pb(models, sample_size=5)

        with mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None)):
            _run(stub._do_benchmark(benchmark_pb))

        assert benchmark_pb.status == service_pb2.ResponseCode.SUCCESS
        assert benchmark_pb.winner in {"a/1", "b/2"}
        assert len(benchmark_pb.match_history) == 1
        assert benchmark_pb.match_history[0].round == 1

    def test_exception_in_match_marks_benchmark_as_error(self):
        stub = _StubBenchmarks()
        models = [service_pb2.Model(id="a/1"), service_pb2.Model(id="b/2")]
        benchmark_pb = _make_benchmark_pb(models)

        async def boom(*args, **kwargs):
            raise RuntimeError("inference exploded")
            yield  # make it a generator

        stub.CreateTestRun = boom

        with mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None)):
            _run(stub._do_benchmark(benchmark_pb))

        assert benchmark_pb.status == service_pb2.ResponseCode.ERROR

    def test_cancelled_run_does_not_hang(self):
        stub = _StubBenchmarks()
        models = [service_pb2.Model(id="a/1"), service_pb2.Model(id="b/2")]
        benchmark_pb = _make_benchmark_pb(models, sample_size=5)

        async def cancelled_run(request, context):
            run_pb = service_pb2.TestRun()
            run_pb.id = request.id
            run_pb.status = service_pb2.ResponseCode.CANCELLED
            return run_pb

        stub.GetTestRun = cancelled_run

        with mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None)):
            _run(stub._do_benchmark(benchmark_pb))

        assert benchmark_pb.status == service_pb2.ResponseCode.SUCCESS
        assert benchmark_pb.winner in {"a/1", "b/2"}

    def test_poll_timeout_does_not_hang(self):
        stub = _StubBenchmarks()
        models = [service_pb2.Model(id="a/1"), service_pb2.Model(id="b/2")]
        benchmark_pb = _make_benchmark_pb(models, sample_size=5)

        async def stuck_run(request, context):
            run_pb = service_pb2.TestRun()
            run_pb.id = request.id
            run_pb.status = service_pb2.ResponseCode.IN_PROGRESS
            return run_pb

        stub.GetTestRun = stuck_run

        # Patching time.monotonic itself would break the event loop's own
        # clock; a negative timeout makes the first poll iteration bail.
        with (
            mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None)),
            mock.patch(
                "server.objects.benchmarks._MATCH_RUN_TIMEOUT",
                -1,
            ),
        ):
            _run(stub._do_benchmark(benchmark_pb))

        assert benchmark_pb.status == service_pb2.ResponseCode.SUCCESS


# ---------------------------------------------------------------------------
# _reconstruct_bracket — state recovery
# ---------------------------------------------------------------------------


class TestReconstructBracket:
    def test_empty_history_returns_all_models(self):
        models = [service_pb2.Model(id=f"m/{i}") for i in range(4)]
        bp = _make_benchmark_pb(models)

        competitors, round_num, prior = _reconstruct_bracket(bp)

        assert round_num == 1
        assert prior == []
        assert set(competitors) == {"m/0", "m/1", "m/2", "m/3"}

    def test_complete_round_advances(self):
        models = [service_pb2.Model(id=f"m/{i}") for i in range(4)]
        bp = _make_benchmark_pb(models)

        # Round 1: m/0 beat m/1, m/2 beat m/3
        m1 = service_pb2.BenchmarkMatch(round=1, id=1, winner="m/0")
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/0"))
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/1"))

        m2 = service_pb2.BenchmarkMatch(round=1, id=2, winner="m/2")
        m2.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/2"))
        m2.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/3"))

        bp.match_history.extend([m1, m2])

        competitors, round_num, prior = _reconstruct_bracket(bp)

        assert round_num == 2
        assert prior == []
        assert set(competitors) == {"m/0", "m/2"}

    def test_partial_round_returns_remaining(self):
        models = [service_pb2.Model(id=f"m/{i}") for i in range(4)]
        bp = _make_benchmark_pb(models)

        # Round 1: only first match done (m/0 beat m/1)
        m1 = service_pb2.BenchmarkMatch(round=1, id=1, winner="m/0")
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/0"))
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/1"))
        bp.match_history.extend([m1])

        competitors, round_num, prior = _reconstruct_bracket(bp)

        assert round_num == 1
        assert prior == ["m/0"]
        assert set(competitors) == {"m/2", "m/3"}

    def test_resume_from_complete_round_runs_to_completion(self):
        stub = _StubBenchmarks()
        models = [service_pb2.Model(id=f"m/{i}") for i in range(4)]
        bp = _make_benchmark_pb(models, sample_size=5)

        # Pre-populate round 1 complete
        m1 = service_pb2.BenchmarkMatch(round=1, id=1, winner="m/0")
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/0"))
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/1"))

        m2 = service_pb2.BenchmarkMatch(round=1, id=2, winner="m/2")
        m2.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/2"))
        m2.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/3"))

        bp.match_history.extend([m1, m2])

        with mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None)):
            _run(stub._do_benchmark(bp))

        assert bp.status == service_pb2.ResponseCode.SUCCESS
        assert bp.winner in {"m/0", "m/2"}
        # Round 1 (2 pre-existing) + Round 2 (1 new)
        assert len(bp.match_history) == 3
        assert bp.match_history[2].round == 2

    def test_resume_from_partial_round_completes_remaining(self):
        stub = _StubBenchmarks()
        models = [service_pb2.Model(id=f"m/{i}") for i in range(4)]
        bp = _make_benchmark_pb(models, sample_size=5)

        # Round 1: only first match done
        m1 = service_pb2.BenchmarkMatch(round=1, id=1, winner="m/0")
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/0"))
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/1"))
        bp.match_history.extend([m1])

        with mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None)):
            _run(stub._do_benchmark(bp))

        assert bp.status == service_pb2.ResponseCode.SUCCESS
        assert bp.winner in {"m/0", "m/2", "m/3"}
        # Round 1 match 1 (pre-existing) + Round 1 match 2 (new) + Round 2 final
        assert len(bp.match_history) == 3
        # The second match in round 1 should involve m/2 and m/3
        r1_matches = [m for m in bp.match_history if m.round == 1]
        assert len(r1_matches) == 2
        r1_responders = set()
        for m in r1_matches:
            for r in m.responses:
                r1_responders.add(r.responder)
        assert r1_responders == {"m/0", "m/1", "m/2", "m/3"}

    def test_bye_in_partial_round(self):
        models = [service_pb2.Model(id=f"m/{i}") for i in range(3)]
        bp = _make_benchmark_pb(models)

        # Round 1: first match done (m/0 beat m/1), m/2 gets BYE but not yet recorded
        m1 = service_pb2.BenchmarkMatch(round=1, id=1, winner="m/0")
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/0"))
        m1.responses.append(service_pb2.BenchmarkMatchResponse(responder="m/1"))
        bp.match_history.extend([m1])

        competitors, round_num, prior = _reconstruct_bracket(bp)

        assert round_num == 1
        assert prior == ["m/0"]
        # m/2 is the only remaining competitor (will get a BYE)
        assert competitors == ["m/2"]


# ---------------------------------------------------------------------------
# _advisory_lock_key — cross-process stability
# ---------------------------------------------------------------------------


class TestAdvisoryLockKey:
    def test_deterministic_for_same_id(self):
        assert _advisory_lock_key("bench-1") == _advisory_lock_key("bench-1")

    def test_differs_for_different_ids(self):
        assert _advisory_lock_key("bench-1") != _advisory_lock_key("bench-2")

    def test_known_value_is_seed_independent(self):
        # Pinned value: would change if the derivation ever falls back to
        # Python's seed-randomized hash() (the bug this function fixes).
        expected = int.from_bytes(
            __import__("hashlib").sha256(b"bench-1").digest()[:8],
            "big",
            signed=True,
        )
        assert _advisory_lock_key("bench-1") == expected

    def test_fits_postgres_bigint(self):
        for bid in ("a", "bench-1", "f" * 64):
            key = _advisory_lock_key(bid)
            assert -(2**63) <= key < 2**63


# ---------------------------------------------------------------------------
# CreateBenchmark — budget gate
# ---------------------------------------------------------------------------


def _priced_model(model_id: str, input_cost: float = 0.001) -> service_pb2.Model:
    m = service_pb2.Model(id=model_id)
    m.pricing.input_token_cost = input_cost
    return m


class TestCreateBenchmarkBudgetGate:
    def test_aborts_when_projected_cost_exceeds_budget(self):
        stub = _StubBenchmarks()
        # $1.00 daily limit, nothing spent yet.
        stub._get_user_rate_limits = mock.AsyncMock(
            return_value=service_pb2.UserRateLimits(daily_spend=100)
        )
        stub._get_current_spend = mock.AsyncMock(return_value=(0.0, 0.0))

        models = [_priced_model("a/1"), _priced_model("b/2")]
        request = service_pb2.BenchmarkRequest(
            test_id="eval-1", models=models, sample_size=10
        )
        # Projected: 1 match per model * 10 items * 2 * (600 * 0.001) = $12

        with pytest.raises(_Aborted) as exc_info:
            _run(_drain(stub.CreateBenchmark(request=request, context=_make_context())))
        assert exc_info.value.code == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert "benchmark" in exc_info.value.message

    def test_proceeds_when_budget_sufficient(self):
        stub = _StubBenchmarks()
        # $100.00 daily limit comfortably covers the ~$12 projection.
        stub._get_user_rate_limits = mock.AsyncMock(
            return_value=service_pb2.UserRateLimits(daily_spend=10000)
        )
        stub._get_current_spend = mock.AsyncMock(return_value=(0.0, 0.0))

        models = [_priced_model("a/1"), _priced_model("b/2")]
        request = service_pb2.BenchmarkRequest(
            test_id="eval-1", models=models, sample_size=10
        )

        with mock.patch("asyncio.create_task"):
            results = _run(
                _drain(stub.CreateBenchmark(request=request, context=_make_context()))
            )
        assert len(results) == 1
        assert results[0].code == service_pb2.ResponseCode.IN_PROGRESS

    def test_unpriced_models_skip_the_gate(self):
        stub = _StubBenchmarks()
        stub._get_user_rate_limits = mock.AsyncMock(
            return_value=service_pb2.UserRateLimits(daily_spend=100)
        )
        stub._get_current_spend = mock.AsyncMock(return_value=(0.0, 0.0))

        models = [service_pb2.Model(id="a/1"), service_pb2.Model(id="b/2")]
        request = service_pb2.BenchmarkRequest(
            test_id="eval-1", models=models, sample_size=10
        )

        with mock.patch("asyncio.create_task"):
            results = _run(
                _drain(stub.CreateBenchmark(request=request, context=_make_context()))
            )
        assert len(results) == 1
        assert results[0].code == service_pb2.ResponseCode.IN_PROGRESS

    def test_reasoning_raises_projection_past_budget(self):
        stub = _StubBenchmarks()
        # $15 budget: covers the $12 base projection but not the
        # reasoning-inclusive one ($12 + 1 * 10 * 2 * 700 * 0.001 = $26).
        stub._get_user_rate_limits = mock.AsyncMock(
            return_value=service_pb2.UserRateLimits(daily_spend=1500)
        )
        stub._get_current_spend = mock.AsyncMock(return_value=(0.0, 0.0))

        models = [_priced_model("a/1"), _priced_model("b/2")]
        request = service_pb2.BenchmarkRequest(
            test_id="eval-1", models=models, sample_size=10, include_reasoning=True
        )

        with pytest.raises(_Aborted) as exc_info:
            _run(_drain(stub.CreateBenchmark(request=request, context=_make_context())))
        assert exc_info.value.code == grpc.StatusCode.RESOURCE_EXHAUSTED


# ---------------------------------------------------------------------------
# Benchmark reasoning — persistence and propagation to match runs
# ---------------------------------------------------------------------------


class TestBenchmarkReasoning:
    def test_include_reasoning_persisted_on_benchmark(self):
        stub = _StubBenchmarks()
        created_protos = []
        original_create = stub._grpc_create

        async def capture_create(**kwargs):
            if kwargs.get("obj_type") == "benchmarks":
                created_protos.append(kwargs["proto_obj"])
            return await original_create(**kwargs)

        stub._grpc_create = capture_create

        with mock.patch("asyncio.create_task"):
            models = [service_pb2.Model(id="a/1"), service_pb2.Model(id="b/2")]
            request = service_pb2.BenchmarkRequest(
                test_id="eval-1", models=models, include_reasoning=True
            )
            _run(_drain(stub.CreateBenchmark(request=request, context=_make_context())))

        assert len(created_protos) == 1
        assert created_protos[0].include_reasoning is True

    def test_match_runs_inherit_reasoning_flag(self):
        stub = _StubBenchmarks()
        match_requests = []

        async def capture_run(request, context):
            match_requests.append(request)
            reply = service_pb2.TestRunReply()
            reply.run_id = "run-1"
            reply.code = service_pb2.ResponseCode.IN_PROGRESS
            yield reply

        stub.CreateTestRun = capture_run

        models = [service_pb2.Model(id="a/1"), service_pb2.Model(id="b/2")]
        benchmark_pb = _make_benchmark_pb(models, sample_size=5)
        benchmark_pb.include_reasoning = True

        with mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None)):
            _run(stub._do_benchmark(benchmark_pb))

        assert len(match_requests) == 1
        assert match_requests[0].include_reasoning is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
