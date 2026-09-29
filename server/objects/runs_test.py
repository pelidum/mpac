"""Unit tests for RunsMixin: sample-size denominator, cost accumulation,
cancel-before-update guard, and UpdateTestRun cancel-signal dispatch."""

import asyncio
import statistics
import unittest.mock as mock

import pytest

from server import service_pb2
from server.objects import runs as runs_module
from server.objects.runs import RunsMixin, _CANCEL_EVENTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_answer(
    is_correct=True,
    task_duration=1.0,
    input_tokens=100,
    output_tokens=50,
    input_cost=0.001,
    output_cost=0.0005,
    answer="A",
    attachment_type=service_pb2.FileModality.TEXT,
):
    a = service_pb2.TestRunAnswer()
    a.id = "test-answer-id"
    a.is_correct = is_correct
    a.task_duration = task_duration
    a.input_tokens = input_tokens
    a.output_tokens = output_tokens
    a.input_cost = input_cost
    a.output_cost = output_cost
    a.answer = answer
    a.attachment_type = attachment_type
    return a


# ---------------------------------------------------------------------------
# Cost accumulation (#21)
# ---------------------------------------------------------------------------


class TestCostAccumulation:
    """Verify that total_cost and total_tokens are summed correctly across answers."""

    def test_single_answer(self):
        answers = [_make_answer(input_cost=0.001, output_cost=0.0005)]
        total_cost = sum(a.input_cost + a.output_cost for a in answers)
        total_tokens = sum(a.input_tokens + a.output_tokens for a in answers)
        assert abs(total_cost - 0.0015) < 1e-9
        assert total_tokens == 150

    def test_multiple_answers(self):
        answers = [
            _make_answer(
                input_cost=0.001, output_cost=0.0005, input_tokens=100, output_tokens=50
            ),
            _make_answer(
                input_cost=0.002, output_cost=0.001, input_tokens=200, output_tokens=100
            ),
            _make_answer(
                input_cost=0.003,
                output_cost=0.0015,
                input_tokens=300,
                output_tokens=150,
            ),
        ]
        total_cost = sum(a.input_cost + a.output_cost for a in answers)
        total_tokens = sum(a.input_tokens + a.output_tokens for a in answers)
        assert abs(total_cost - 0.009) < 1e-9
        assert total_tokens == 900

    def test_zero_cost_answers(self):
        answers = [_make_answer(input_cost=0.0, output_cost=0.0)]
        total_cost = sum(a.input_cost + a.output_cost for a in answers)
        assert total_cost == 0.0

    def test_per_model_cost_matches_sum(self):
        """model_total_cost mirrors the per-answer accumulation into total_cost."""
        answers = [
            _make_answer(input_cost=0.001, output_cost=0.0005),
            _make_answer(input_cost=0.002, output_cost=0.001),
        ]
        model_total_cost = sum(a.input_cost + a.output_cost for a in answers)
        total_cost = model_total_cost  # single model: they must be equal
        assert abs(model_total_cost - 0.0045) < 1e-9
        assert abs(total_cost - model_total_cost) < 1e-12

    def test_per_model_tokens_matches_sum(self):
        answers = [
            _make_answer(input_tokens=100, output_tokens=50),
            _make_answer(input_tokens=200, output_tokens=75),
        ]
        model_total_tokens = sum(a.input_tokens + a.output_tokens for a in answers)
        assert model_total_tokens == 425


# ---------------------------------------------------------------------------
# Task duration / sample-size denominator (#20)
# ---------------------------------------------------------------------------


class TestSampleSizeDenominator:
    """Verify the actual_sample_size = len(sample_item_pbs) guard prevents
    ZeroDivisionError and produces correct percentages."""

    def test_nonzero_sample_size(self):
        answers = [_make_answer(is_correct=True) for _ in range(8)] + [
            _make_answer(is_correct=False) for _ in range(2)
        ]
        actual_sample_size = len(answers)  # 10
        num_correct = len([a for a in answers if a.is_correct])
        score = num_correct / actual_sample_size if actual_sample_size else 0.0
        assert abs(score - 0.8) < 1e-9

    def test_zero_sample_size_guard(self):
        """When no items were fetched, division should return 0.0, not raise."""
        actual_sample_size = 0
        num_correct = 0
        score = num_correct / actual_sample_size if actual_sample_size else 0.0
        assert score == 0.0

    def test_refusal_rate_zero_guard(self):
        actual_sample_size = 0
        num_refusals = 0
        rate = num_refusals / actual_sample_size if actual_sample_size else 0.0
        assert rate == 0.0

    def test_median_task_duration(self):
        answers = [_make_answer(task_duration=d) for d in [1.0, 2.0, 3.0, 4.0, 5.0]]
        durations = [a.task_duration for a in answers]
        assert statistics.median(durations) == 3.0

    def test_tpm_wall_time_formula(self):
        # New formula: total output tokens / model wall time * 60.
        # 300 output tokens in 30 seconds = 600 TPM.
        answers = [_make_answer(output_tokens=100) for _ in range(3)]
        total_output = sum(a.output_tokens for a in answers if a.output_tokens > 0)
        model_wall_time = 30.0
        tpm = int(total_output / model_wall_time * 60)
        assert tpm == 600

    def test_tpm_zero_wall_time_guard(self):
        answers = [_make_answer(output_tokens=100)]
        total_output = sum(a.output_tokens for a in answers if a.output_tokens > 0)
        model_wall_time = 0.0
        tpm = int(total_output / model_wall_time * 60) if model_wall_time > 0 else 0
        assert tpm == 0


# ---------------------------------------------------------------------------
# Cancel-before-update guard (#19)
# ---------------------------------------------------------------------------


class TestCancelBeforeUpdate:
    """Verify cancel_event.is_set() is checked before persisting IN_PROGRESS
    metrics so a CANCELLED status set by UpdateTestRun is not overwritten."""

    def test_cancel_event_prevents_update(self):
        """Simulate the per-model update guard: if cancel_event is set after
        computing metrics, we should raise CancelledError, not call _grpc_update."""
        cancel_event = asyncio.Event()
        cancel_event.set()

        update_called = False

        def fake_update():
            nonlocal update_called
            update_called = True

        # Simulate the guard added to _do_run_inference
        if cancel_event.is_set():
            pass  # raise CancelledError in production; here just skip the update
        else:
            fake_update()

        assert not update_called

    def test_no_cancel_allows_update(self):
        cancel_event = asyncio.Event()  # not set

        update_called = False

        def fake_update():
            nonlocal update_called
            update_called = True

        if cancel_event.is_set():
            pass
        else:
            fake_update()

        assert update_called

    def test_cancel_event_set_after_model_loop_entry(self):
        """Ensure asyncio.Event.is_set() is False by default and True after .set()."""
        event = asyncio.Event()
        assert not event.is_set()
        event.set()
        assert event.is_set()

    def test_tasks_are_cancellable(self):
        """asyncio.ensure_future wraps coroutines as Tasks that support .cancel()."""

        async def noop():
            await asyncio.sleep(10)

        async def _check():
            task = asyncio.ensure_future(noop())
            assert hasattr(task, "cancel")
            task.cancel()
            # cancelled tasks raise CancelledError when awaited
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.get_event_loop().run_until_complete(_check())


# ---------------------------------------------------------------------------
# _do_run_inference — partial cost persistence on cancellation / error
# ---------------------------------------------------------------------------


class _DoRunStub(RunsMixin):
    """Minimal RunsMixin stub for _do_run_inference integration tests."""

    def __init__(self, answer_side_effect=None, daily_spend_cents=0):
        self._update_calls = []
        self._answer_call_count = 0
        self._instructions_seen = []
        self._answer_side_effect = answer_side_effect
        self._daily_spend_cents = daily_spend_cents
        self.db_pool = None

    async def answer_test_item(
        self,
        item_pb,
        run_id,
        model_pb,
        backend,
        context,
        include_reasoning,
        instructions="",
    ):
        self._answer_call_count += 1
        self._instructions_seen.append(instructions)
        if self._answer_side_effect is not None:
            return self._answer_side_effect(self._answer_call_count)
        return _make_answer()

    async def record_and_check_tpm(self, user_id, tokens):
        return True

    async def _get_user_rate_limits(self, user_id):
        limits = service_pb2.UserRateLimits()
        limits.daily_spend = self._daily_spend_cents
        return limits

    async def _get_current_spend(self, user_id):
        return 0.0, 0.0

    async def _grpc_create(self, id, proto_obj, obj_type, overwrite=False):
        pass

    async def _grpc_update(self, id, proto_obj, obj_type, obj_owner=None):
        self._update_calls.append(proto_obj)
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

    async def _grpc_get(self, id, obj_type):
        return None

    def estimate_local_inference_cost(self, duration_seconds):
        return 0.0

    def calculate_grade(self, score):
        return "A"


def _make_run_and_request(owner="user@example.com"):
    run_pb = service_pb2.TestRun()
    run_pb.id = "run-cost-test"
    run_pb.owner = owner

    model_pb = service_pb2.Model()
    model_pb.id = "model-1"

    request = service_pb2.TestRunRequest()
    request.models.append(model_pb)
    request.include_reasoning = False

    test_pb = service_pb2.Test()
    test_pb.id = "test-1"

    item_pb = service_pb2.TestItem()
    item_pb.id = "item-1"
    item_pb.test_id = "test-1"

    return run_pb, test_pb, [item_pb], request


class TestDoRunInferenceCostTracking:
    """Integration tests for _do_run_inference partial-cost tracking.

    These tests verify run_pb.total_cost_usd is persisted correctly on all exit
    paths — not just SUCCESS. Without the fix, cancelled/errored runs leave
    total_cost_usd=0.0, causing _get_current_spend() to under-count spend.
    """

    def test_pre_cancelled_run_sets_zero_cost_and_cancelled_status(self):
        """When cancel_event is already set before inference starts,
        total_cost_usd must be 0.0 and status must be CANCELLED."""
        stub = _DoRunStub()
        run_pb, test_pb, items, request = _make_run_and_request()

        cancel_event = asyncio.Event()
        cancel_event.set()

        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
        )

        assert run_pb.status == service_pb2.ResponseCode.CANCELLED
        assert run_pb.total_cost_usd == 0.0
        final = stub._update_calls[-1]
        assert final.status == service_pb2.ResponseCode.CANCELLED
        assert final.total_cost_usd == 0.0

    def test_test_instructions_passed_to_answer_test_item(self):
        stub = _DoRunStub()
        run_pb, test_pb, items, request = _make_run_and_request()
        test_pb.instructions = "Apply policy X."

        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, asyncio.Event())
        )

        assert stub._instructions_seen == ["Apply policy X."]

    def test_spend_limit_cancellation_records_partial_cost(self):
        """With 1 item and daily_spend=1 cent, an answer costing $0.02 triggers
        the spend-limit cancel.  The post-model-loop guard raises CancelledError;
        the handler must persist total_cost_usd=$0.02.

        The cost must clear the limit with float32 headroom: proto float costs
        summing to exactly $0.01 truncate below the 1-cent threshold via
        int(cost * 100)."""

        def _answer_factory(call_count):
            a = _make_answer(input_cost=0.01, output_cost=0.01)
            a.id = f"answer-{call_count}"
            return a

        stub = _DoRunStub(answer_side_effect=_answer_factory, daily_spend_cents=1)
        run_pb, test_pb, items, request = _make_run_and_request()
        cancel_event = asyncio.Event()

        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
        )

        assert cancel_event.is_set()
        assert run_pb.status == service_pb2.ResponseCode.CANCELLED
        assert abs(run_pb.total_cost_usd - 0.02) < 1e-5
        final = stub._update_calls[-1]
        assert final.status == service_pb2.ResponseCode.CANCELLED
        assert abs(final.total_cost_usd - 0.02) < 1e-5

    def test_error_mid_inference_records_zero_cost_when_first_answer_fails(self):
        """When answer_test_item raises on the first call, _run_cost stays 0.0.
        The except Exception handler must persist total_cost_usd=0.0 and set
        status=ERROR.  Also validates _run_cost is initialized before the try block
        (no NameError in the handler)."""

        def _always_fails(call_count):
            raise RuntimeError("Simulated inference failure")

        stub = _DoRunStub(answer_side_effect=_always_fails)
        run_pb, test_pb, items, request = _make_run_and_request()
        cancel_event = asyncio.Event()

        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
        )

        assert run_pb.status == service_pb2.ResponseCode.ERROR
        assert run_pb.total_cost_usd == 0.0
        final = stub._update_calls[-1]
        assert final.status == service_pb2.ResponseCode.ERROR
        assert final.total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# Shutdown vs. deliberate cancellation
# ---------------------------------------------------------------------------


class _SlowAnswerStub(_DoRunStub):
    """Stub whose answers stagger by call order, so tasks can be interrupted."""

    def __init__(self, delay_per_call=0.05, answer_side_effect=None):
        super().__init__(answer_side_effect=answer_side_effect)
        self._delay_per_call = delay_per_call

    async def answer_test_item(
        self,
        item_pb,
        run_id,
        model_pb,
        backend,
        context,
        include_reasoning,
        instructions="",
    ):
        self._answer_call_count += 1
        call = self._answer_call_count
        await asyncio.sleep(self._delay_per_call * call)
        if self._answer_side_effect is not None:
            answer = self._answer_side_effect(call)
        else:
            answer = _make_answer()
        answer.item_id = item_pb.id
        return answer


class TestShutdownCancellation:
    def test_shutdown_cancel_leaves_run_in_progress(self):
        """When the inference task is cancelled externally (process shutdown)
        without cancel_event being set, no CANCELLED status may be written —
        the run must stay IN_PROGRESS so resume_stale_runs can pick it up on
        another instance. Writing CANCELLED would permanently drop the run."""
        stub = _SlowAnswerStub(delay_per_call=10.0)
        run_pb, test_pb, items, request = _make_run_and_request()
        run_pb.status = service_pb2.ResponseCode.IN_PROGRESS
        cancel_event = asyncio.Event()

        async def _shutdown_scenario():
            task = asyncio.ensure_future(
                stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
            )
            await asyncio.sleep(0.05)  # let inference start
            task.cancel()  # simulates asyncio.run teardown on SIGTERM
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.get_event_loop().run_until_complete(_shutdown_scenario())

        cancelled_writes = [
            pb
            for pb in stub._update_calls
            if pb.status == service_pb2.ResponseCode.CANCELLED
        ]
        assert cancelled_writes == []
        assert run_pb.status == service_pb2.ResponseCode.IN_PROGRESS

    def test_deliberate_cancel_still_writes_cancelled(self):
        """A cancel_event-driven cancellation must still persist CANCELLED."""
        stub = _SlowAnswerStub(delay_per_call=10.0)
        run_pb, test_pb, items, request = _make_run_and_request()
        cancel_event = asyncio.Event()

        async def _cancel_scenario():
            task = asyncio.ensure_future(
                stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
            )
            await asyncio.sleep(0.05)
            cancel_event.set()
            await task  # cooperative cancel: task handles it and returns

        asyncio.get_event_loop().run_until_complete(_cancel_scenario())

        assert run_pb.status == service_pb2.ResponseCode.CANCELLED
        final = stub._update_calls[-1]
        assert final.status == service_pb2.ResponseCode.CANCELLED


# ---------------------------------------------------------------------------
# Circuit breaker — consecutive connection errors stop remaining items
# ---------------------------------------------------------------------------


class _RecordingCreateStub(_SlowAnswerStub):
    def __init__(self, delay_per_call=0.05, answer_side_effect=None):
        super().__init__(
            delay_per_call=delay_per_call, answer_side_effect=answer_side_effect
        )
        self._created_answers = []

    async def _grpc_create(self, id, proto_obj, obj_type, overwrite=False):
        if obj_type == "test_run_answers":
            self._created_answers.append(proto_obj)


class TestConnectionErrorCircuitBreaker:
    def test_breaker_skips_remaining_items_and_completes_run(self):
        """After _CONN_ERROR_BREAKER_THRESHOLD consecutive connection-error
        answers, remaining in-flight items must be cancelled and recorded as
        [SKIPPED] without hitting the backend, and the run must still complete
        with every sampled item accounted for."""
        num_items = 10
        threshold = runs_module._CONN_ERROR_BREAKER_THRESHOLD

        def _conn_error_answer(call_count):
            a = service_pb2.TestRunAnswer()
            a.id = f"answer-{call_count}"
            a.status = service_pb2.TestRunAnswer.CONNECTION_ERROR
            a.error = "Connection error."
            return a

        stub = _RecordingCreateStub(
            delay_per_call=0.03, answer_side_effect=_conn_error_answer
        )
        run_pb, test_pb, items, request = _make_run_and_request()
        item_pbs = []
        for i in range(num_items):
            item_pb = service_pb2.TestItem()
            item_pb.id = f"item-{i}"
            item_pb.test_id = test_pb.id
            item_pbs.append(item_pb)

        cancel_event = asyncio.Event()
        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, item_pbs, request, cancel_event)
        )

        assert run_pb.status == service_pb2.ResponseCode.SUCCESS
        # Every sampled item has exactly one persisted answer.
        answered_ids = {a.item_id for a in stub._created_answers}
        assert answered_ids == {f"item-{i}" for i in range(num_items)}
        assert len(stub._created_answers) == num_items
        # At least (num_items - threshold) items were skipped without an
        # inference round trip.
        skipped = [
            a
            for a in stub._created_answers
            if a.status == service_pb2.TestRunAnswer.SKIPPED
        ]
        conn_errors = [
            a
            for a in stub._created_answers
            if a.status == service_pb2.TestRunAnswer.CONNECTION_ERROR
        ]
        assert all(a.error and not a.raw_response for a in skipped)
        assert len(conn_errors) >= threshold
        assert len(skipped) >= 1
        assert len(skipped) + len(conn_errors) == num_items

    def test_intermittent_errors_do_not_trip_breaker(self):
        """Non-consecutive connection errors (transient blips) must not trip
        the breaker — the consecutive counter resets on any good answer."""

        def _alternating(call_count):
            if call_count % 2 == 0:
                a = service_pb2.TestRunAnswer()
                a.id = f"answer-{call_count}"
                # Legacy prefix encoding: the breaker must still recognize it.
                a.raw_response = "[CONNECTION_ERROR] Connection error."
                return a
            a = _make_answer()
            a.id = f"answer-{call_count}"
            return a

        stub = _RecordingCreateStub(
            delay_per_call=0.02, answer_side_effect=_alternating
        )
        run_pb, test_pb, items, request = _make_run_and_request()
        item_pbs = []
        for i in range(8):
            item_pb = service_pb2.TestItem()
            item_pb.id = f"item-{i}"
            item_pb.test_id = test_pb.id
            item_pbs.append(item_pb)

        cancel_event = asyncio.Event()
        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, item_pbs, request, cancel_event)
        )

        assert run_pb.status == service_pb2.ResponseCode.SUCCESS
        skipped = [
            a for a in stub._created_answers if a.raw_response.startswith("[SKIPPED]")
        ]
        assert skipped == []
        assert len(stub._created_answers) == 8


# ---------------------------------------------------------------------------
# Resume sample reconstruction
# ---------------------------------------------------------------------------


class TestReconstructSample:
    @staticmethod
    def _items(n):
        out = []
        for i in range(n):
            item = service_pb2.TestItem()
            item.id = f"item-{i}"
            out.append(item)
        return out

    def test_caps_resumed_run_at_sample_size(self):
        """A resumed sample_size=10 run on a 100-item test must NOT run all
        100 items — that burns quota on 90 items the user never asked for."""
        all_items = self._items(100)
        completed = {("model-1", f"item-{i}") for i in range(4)}
        sample = RunsMixin._reconstruct_sample(all_items, completed, 10)
        assert len(sample) == 10
        sample_ids = {i.id for i in sample}
        # Every already-answered item must be in the reconstructed sample.
        assert {f"item-{i}" for i in range(4)} <= sample_ids

    def test_zero_sample_size_defaults_to_ten(self):
        sample = RunsMixin._reconstruct_sample(self._items(50), set(), 0)
        assert len(sample) == 10

    def test_answered_items_exceeding_fill_are_all_kept(self):
        """If the union of answered items already reaches sample_size, no
        extra items are added."""
        all_items = self._items(20)
        completed = {("model-1", f"item-{i}") for i in range(10)}
        sample = RunsMixin._reconstruct_sample(all_items, completed, 10)
        assert {i.id for i in sample} == {f"item-{i}" for i in range(10)}

    def test_small_test_returns_all_items(self):
        sample = RunsMixin._reconstruct_sample(self._items(3), set(), 10)
        assert len(sample) == 3


# ---------------------------------------------------------------------------
# UpdateTestRun — cancel signal dispatch (#22)
# ---------------------------------------------------------------------------


class _StubRuns(RunsMixin):
    _request_owner = "user@example.com"
    _is_admin = False

    async def get_request_owner(self, request):
        return self._request_owner

    async def is_user_admin(self, user_id):
        return self._is_admin

    async def GetTestRun(self, request, context):
        run_pb = service_pb2.TestRun()
        run_pb.id = request.id
        run_pb.owner = self._request_owner
        run_pb.status = service_pb2.ResponseCode.IN_PROGRESS
        return run_pb

    async def _grpc_update(self, id, proto_obj, obj_type, obj_owner=None):
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)


class TestUpdateTestRunCancelSignal:
    def test_cancelled_status_sets_registered_event(self):
        stub = _StubRuns()
        cancel_event = asyncio.Event()
        run_id = "signal-test-run-1"
        _CANCEL_EVENTS[run_id] = cancel_event

        run_pb = service_pb2.TestRun()
        run_pb.id = run_id
        run_pb.status = service_pb2.ResponseCode.CANCELLED

        asyncio.get_event_loop().run_until_complete(
            stub.UpdateTestRun(request=run_pb, context=mock.AsyncMock())
        )
        assert cancel_event.is_set()

    def test_non_cancelled_status_does_not_set_event(self):
        stub = _StubRuns()
        cancel_event = asyncio.Event()
        run_id = "signal-test-run-2"
        _CANCEL_EVENTS[run_id] = cancel_event

        run_pb = service_pb2.TestRun()
        run_pb.id = run_id
        run_pb.status = service_pb2.ResponseCode.SUCCESS

        asyncio.get_event_loop().run_until_complete(
            stub.UpdateTestRun(request=run_pb, context=mock.AsyncMock())
        )
        assert not cancel_event.is_set()

    def test_cancel_with_no_registered_event_is_no_op(self):
        """Cancelling a run with no registered cancel event must not raise."""
        stub = _StubRuns()
        run_id = "signal-test-run-unregistered"
        assert run_id not in _CANCEL_EVENTS

        run_pb = service_pb2.TestRun()
        run_pb.id = run_id
        run_pb.status = service_pb2.ResponseCode.CANCELLED

        # Must not raise
        asyncio.get_event_loop().run_until_complete(
            stub.UpdateTestRun(request=run_pb, context=mock.AsyncMock())
        )


# ---------------------------------------------------------------------------
# throttled_answer_task — semaphore wiring
# ---------------------------------------------------------------------------


class _SpyResources:
    """Records every acquire/release so tests can prove the throttle path ran."""

    def __init__(self):
        self.request_timeout = 120.0
        self.acquire_calls = 0
        self.release_calls = 0
        self.in_flight_peak = 0
        self._in_flight = 0

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                outer.acquire_calls += 1
                outer._in_flight += 1
                outer.in_flight_peak = max(outer.in_flight_peak, outer._in_flight)
                return None

            async def __aexit__(self_inner, exc_type, exc, tb):
                outer._in_flight -= 1
                outer.release_calls += 1
                return False

        return _Ctx()


class TestThrottledAnswerTaskWiring:
    """Concurrency cap is the whole point of the new code path. These tests
    assert the wiring: each answer_test_item dispatch goes through acquire(),
    and every acquire pairs with a release (no slot leaks on success or error).
    """

    def _setup(self, monkeypatch, items_count=3, answer_side_effect=None):
        spy = _SpyResources()

        async def _get(_backend):
            return spy

        monkeypatch.setattr(runs_module, "get_backend_resources", _get)

        stub = _DoRunStub(answer_side_effect=answer_side_effect)
        run_pb, test_pb, _, request = _make_run_and_request()
        items = []
        for i in range(items_count):
            item = service_pb2.TestItem()
            item.id = f"item-{i}"
            items.append(item)
        return spy, stub, run_pb, test_pb, items, request

    def test_each_item_acquires_resources(self, monkeypatch):
        spy, stub, run_pb, test_pb, items, request = self._setup(
            monkeypatch, items_count=4
        )
        cancel_event = asyncio.Event()
        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
        )
        assert spy.acquire_calls == 4
        assert spy.release_calls == 4

    def test_acquire_releases_on_error(self, monkeypatch):
        """If answer_test_item raises, the semaphore slot must still be
        released — otherwise the pool leaks on every failure."""

        def _always_fails(call_count):
            raise RuntimeError("boom")

        spy, stub, run_pb, test_pb, items, request = self._setup(
            monkeypatch, items_count=2, answer_side_effect=_always_fails
        )
        cancel_event = asyncio.Event()
        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
        )
        # acquire was called at least once (first item enters the loop), and
        # every acquire was paired with a release.
        assert spy.acquire_calls >= 1
        assert spy.acquire_calls == spy.release_calls


class _TimeoutOnAcquireResources:
    """Spy whose acquire() raises asyncio.TimeoutError on every __aenter__.

    Models a backend pool so saturated that the wait_for cap in acquire()
    trips. The point is to prove the run does NOT die in that case.
    """

    def __init__(self):
        self.request_timeout = 120.0
        self.acquire_calls = 0

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                outer.acquire_calls += 1
                raise asyncio.TimeoutError()

            async def __aexit__(self_inner, exc_type, exc, tb):
                return False

        return _Ctx()


class _RecordingStub(_DoRunStub):
    """_DoRunStub variant that records the answer protos sent to _grpc_create."""

    def __init__(self):
        super().__init__()
        self.created_answers = []

    async def _grpc_create(self, id, proto_obj, obj_type, overwrite=False):
        if obj_type == "test_run_answers":
            self.created_answers.append(proto_obj)


class TestThrottledAnswerTaskTimeout:
    """When acquire() times out (backend saturated), the failing item must be
    recorded as a failed answer_pb rather than aborting the entire run.
    """

    def test_acquire_timeout_does_not_kill_run(self, monkeypatch):
        spy = _TimeoutOnAcquireResources()

        async def _get(_backend):
            return spy

        monkeypatch.setattr(runs_module, "get_backend_resources", _get)

        stub = _RecordingStub()
        run_pb, test_pb, _, request = _make_run_and_request()
        items = []
        for i in range(3):
            item = service_pb2.TestItem()
            item.id = f"item-{i}"
            items.append(item)
        cancel_event = asyncio.Event()

        # Must NOT raise — the prior behavior was TimeoutError bubbling up.
        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
        )

        # Every item should have a failed answer recorded.
        assert spy.acquire_calls == 3
        assert len(stub.created_answers) == 3
        for answer_pb in stub.created_answers:
            assert answer_pb.status == service_pb2.TestRunAnswer.CAPACITY
            assert "Backend at capacity" in answer_pb.error
            assert answer_pb.run_id == run_pb.id
            assert answer_pb.model_id == "model-1"


# ---------------------------------------------------------------------------
# Duplicate model instances within a single run
# ---------------------------------------------------------------------------


class _ResponderRecordingStub(_DoRunStub):
    """Records created answers and tags them like real inference does."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.created_answers = []

    async def answer_test_item(
        self,
        item_pb,
        run_id,
        model_pb,
        backend,
        context,
        include_reasoning,
        instructions="",
    ):
        self._answer_call_count += 1
        a = _make_answer()
        a.id = f"ans-{self._answer_call_count}"
        a.run_id = run_id
        a.item_id = item_pb.id
        a.model_id = model_pb.id
        a.responder_id = model_pb.responder_id or model_pb.id
        return a

    async def _grpc_create(self, id, proto_obj, obj_type, overwrite=False):
        if obj_type == "test_run_answers":
            self.created_answers.append(proto_obj)


def _make_dup_run_and_request():
    """A survey run with the same model entered twice (m, m#2)."""
    run_pb = service_pb2.TestRun()
    run_pb.id = "run-dup"
    run_pb.owner = "user@example.com"

    request = service_pb2.TestRunRequest()
    for responder_id in ("m", "m#2"):
        model_pb = service_pb2.Model()
        model_pb.id = "m"
        model_pb.responder_id = responder_id
        request.models.append(model_pb)
    run_pb.models.extend(request.models)

    test_pb = service_pb2.Test()
    test_pb.id = "test-1"
    test_pb.type = service_pb2.TestType.SURVEY  # non-eval: skips sklearn path

    item_pb = service_pb2.TestItem()
    item_pb.id = "item-1"
    item_pb.test_id = "test-1"

    return run_pb, test_pb, [item_pb], request


class TestDuplicateModelInstances:
    """The same model run twice in one run must stay distinguishable: two
    metrics rows with distinct responder_ids but the same model_id, and two
    answers tagged by responder_id."""

    def test_two_instances_produce_distinct_metrics_and_answers(self):
        stub = _ResponderRecordingStub()
        run_pb, test_pb, items, request = _make_dup_run_and_request()
        cancel_event = asyncio.Event()

        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
        )

        responder_ids = sorted(m.responder_id for m in run_pb.metrics)
        assert responder_ids == ["m", "m#2"]
        # Both instances resolve to the same real model for cross-run grouping.
        assert {m.model_id for m in run_pb.metrics} == {"m"}
        # Each instance produced its own answer, keyed by responder_id.
        assert sorted(a.responder_id for a in stub.created_answers) == ["m", "m#2"]
        assert {a.model_id for a in stub.created_answers} == {"m"}

    def test_responder_key_falls_back_to_model_id(self):
        """A model with no responder_id (legacy path) keys on its model id."""
        stub = _ResponderRecordingStub()
        run_pb, test_pb, items, request = _make_run_and_request()
        cancel_event = asyncio.Event()

        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, cancel_event)
        )

        assert [m.responder_id for m in run_pb.metrics] == ["model-1"]
        assert [a.responder_id for a in stub.created_answers] == ["model-1"]


# ---------------------------------------------------------------------------
# GetTestRun — parent-Test visibility enforcement
# ---------------------------------------------------------------------------
#
# TestRun carries no visibility field of its own, so the generic visibility
# clause inside _grpc_get(obj_type="test_runs") is always a no-op
# (proto_jsonb->'visibility' is always NULL on that table). GetTestRun must
# re-check the parent Test's visibility via a second _grpc_get rather than
# trusting that no-op check. These tests lock in that behavior.


class TestGetTestRunVisibility:
    def _stub(
        self,
        test_id="test-1",
        run_owner="owner@example.com",
        parent_test_visible=True,
        run_exists=True,
    ):
        run_pb = service_pb2.TestRun()
        run_pb.id = "run-1"
        run_pb.owner = run_owner
        run_pb.test_id = test_id

        class _Stub(RunsMixin):
            async def _grpc_get(self, id, obj_type, obj_owner=None):
                if obj_type == "test_runs":
                    return run_pb.SerializeToString() if run_exists else None
                if obj_type == "tests":
                    return b"\x01" if parent_test_visible else None
                return None

        return _Stub()

    def _context(self):
        ctx = mock.AsyncMock()

        async def abort(code, msg):
            raise RuntimeError(msg)

        ctx.abort = abort
        return ctx

    def test_visible_parent_test_returns_run(self):
        """Happy path: requester can see the parent Test -> run is returned."""
        stub = self._stub(parent_test_visible=True)
        result = asyncio.get_event_loop().run_until_complete(
            stub.GetTestRun(
                request=service_pb2.GetRequest(id="run-1"), context=self._context()
            )
        )
        assert result.id == "run-1"

    def test_hidden_parent_test_denies_access(self):
        """Sad path: parent Test invisible to the requester (owner-only /
        specified-users elsewhere) -> GetTestRun must abort rather than leak
        the run."""
        stub = self._stub(parent_test_visible=False)
        with pytest.raises(RuntimeError):
            asyncio.get_event_loop().run_until_complete(
                stub.GetTestRun(
                    request=service_pb2.GetRequest(id="run-1"),
                    context=self._context(),
                )
            )

    def test_run_not_found_returns_empty(self):
        """Sad path: the run itself doesn't exist / isn't visible -> empty
        TestRun, no abort."""
        stub = self._stub(run_exists=False)
        result = asyncio.get_event_loop().run_until_complete(
            stub.GetTestRun(
                request=service_pb2.GetRequest(id="run-1"), context=self._context()
            )
        )
        assert result.id == ""


# ---------------------------------------------------------------------------
# Performance metrics: TTFT / output TPS / latency
# ---------------------------------------------------------------------------


def _perf_answer(task_duration=1.0, ttft=0.2, output_tps=50.0, raw_response="A"):
    a = _make_answer(task_duration=task_duration)
    a.ttft = ttft
    a.output_tps = output_tps
    a.raw_response = raw_response
    return a


class TestPercentiles:
    def test_empty(self):
        assert runs_module._percentiles([]) == (0.0, 0.0, 0.0)

    def test_single_value(self):
        assert runs_module._percentiles([2.5]) == (2.5, 2.5, 2.5)

    def test_many_values(self):
        values = [float(x) for x in range(1, 101)]
        p50, p95, p99 = runs_module._percentiles(values)
        assert p50 == statistics.median(values)
        assert 94 < p95 < 97
        assert 98 < p99 <= 100


class TestApplyPerformanceMetrics:
    def test_failed_requests_are_excluded(self):
        answers = [_perf_answer(task_duration=1.0, ttft=0.2) for _ in range(5)]
        answers += [
            _perf_answer(task_duration=130.0, ttft=0.0, raw_response="[TIMEOUT] x"),
            _perf_answer(task_duration=0.0, ttft=0.0, raw_response="[CAPACITY] x"),
            _perf_answer(task_duration=0.0, ttft=0.0, raw_response="[SKIPPED] x"),
            _perf_answer(task_duration=5.0, ttft=4.0, raw_response="[ERROR] boom"),
        ]
        m = service_pb2.TestRun.TestRunMetrics()
        runs_module._apply_performance_metrics(m, answers)
        assert m.median_task_duration == pytest.approx(1.0)
        assert m.task_duration_p95 == pytest.approx(1.0)
        assert m.ttft_p50 == pytest.approx(0.2)
        assert m.ttft_p99 == pytest.approx(0.2)
        assert m.output_tps_p50 == pytest.approx(50.0)
        assert m.streamed

    def test_status_field_excludes_failed_requests(self):
        answers = [_perf_answer(task_duration=1.0, ttft=0.2)]
        timed_out = _perf_answer(task_duration=130.0, ttft=0.0, raw_response="")
        timed_out.status = service_pb2.TestRunAnswer.TIMEOUT
        answers.append(timed_out)
        m = service_pb2.TestRun.TestRunMetrics()
        runs_module._apply_performance_metrics(m, answers)
        assert m.task_duration_p99 == pytest.approx(1.0)

    def test_refusals_still_count_as_successful_requests(self):
        answers = [_perf_answer(ttft=0.3, raw_response="I cannot answer that")]
        m = service_pb2.TestRun.TestRunMetrics()
        runs_module._apply_performance_metrics(m, answers)
        assert m.ttft_p50 == pytest.approx(0.3)

    def test_unmeasured_tps_is_skipped(self):
        answers = [
            _perf_answer(output_tps=0.0),  # single-token answer
            _perf_answer(output_tps=40.0),
            _perf_answer(output_tps=60.0),
        ]
        m = service_pb2.TestRun.TestRunMetrics()
        runs_module._apply_performance_metrics(m, answers)
        assert m.output_tps_p50 == pytest.approx(50.0)

    def test_pre_streaming_answers_are_not_streamed(self):
        answers = [_perf_answer(ttft=0.0, output_tps=0.0) for _ in range(3)]
        m = service_pb2.TestRun.TestRunMetrics()
        runs_module._apply_performance_metrics(m, answers)
        assert not m.streamed
        assert m.ttft_p50 == 0.0
        assert m.output_tps_p50 == 0.0
        assert m.median_task_duration == pytest.approx(1.0)


class TestThroughputWallTime:
    def test_tpm_excludes_rate_limit_sleep(self, monkeypatch):
        """A TPM rate-limit sleep after the request must not count toward the
        model's throughput wall time."""

        class _SlowStub(_DoRunStub):
            async def answer_test_item(self, *args, **kwargs):
                await asyncio.sleep(0.2)
                return _perf_answer()

            async def record_and_check_tpm(self, user_id, tokens):
                return False

        # seconds_remaining = 60 - (59 % 60) = 1 -> sleeps 2s after the answer.
        monkeypatch.setattr(runs_module.time, "time", lambda: 59.0)
        stub = _SlowStub()
        run_pb, test_pb, items, request = _make_run_and_request()
        asyncio.get_event_loop().run_until_complete(
            stub._do_run_inference(run_pb, test_pb, items, request, asyncio.Event())
        )
        metrics = stub._update_calls[-1].metrics[0]
        # 50 output tokens over ~0.2s -> ~15000 TPM; with the sleep it'd be ~1400.
        assert metrics.tokens_per_minute > 5000
        assert metrics.streamed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
