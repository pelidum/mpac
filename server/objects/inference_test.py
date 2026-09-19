"""Unit tests for InferenceMixin: modality token extraction, think-block extraction,
and debug_random inference."""

import asyncio
import re
import unittest.mock as mock

import pytest
from openai import APIConnectionError

from server import service_pb2
from server.objects import inference as inference_module
from server.objects.inference import InferenceMixin


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _StubInference(InferenceMixin):
    inference_engine = "debug_random"
    openai_async_client = None
    db_pool = None

    async def GetAttachment(self, request, context):
        return service_pb2.FileAttachment()

    async def _validate_answer(self, raw_answer, choices):
        return raw_answer if raw_answer in choices else ""


@pytest.fixture
def stub():
    return _StubInference()


def _make_item(choices=("A", "B", "C"), answer="A", item_id="item-1"):
    item = service_pb2.TestItem()
    item.id = item_id
    item.question = "Pick one"
    item.choices.extend(choices)
    item.answer = answer
    return item


def _make_model(model_id="debug/debug_random_model_1"):
    m = service_pb2.Model()
    m.id = model_id
    return m


_DEBUG_BACKEND = service_pb2.Backend(backend_type=service_pb2.BackendType.DEBUG_RANDOM)


# ---------------------------------------------------------------------------
# _extract_modality_tokens
# ---------------------------------------------------------------------------


class TestExtractModalityTokens:
    def test_all_zero_when_attributes_absent(self, stub):
        usage = mock.MagicMock(spec=[])
        result = stub._extract_modality_tokens(usage)
        assert result == {
            "image": 0,
            "input_audio": 0,
            "output_audio": 0,
            "video": 0,
            "cached": 0,
        }

    def test_reads_present_attributes(self, stub):
        usage = mock.MagicMock()
        usage.image_tokens = 10
        usage.input_audio_tokens = 5
        usage.output_audio_tokens = 3
        usage.video_tokens = 2
        usage.cached_tokens = 1
        result = stub._extract_modality_tokens(usage)
        assert result["image"] == 10
        assert result["input_audio"] == 5
        assert result["output_audio"] == 3
        assert result["video"] == 2
        assert result["cached"] == 1

    def test_none_values_coerced_to_zero(self, stub):
        usage = mock.MagicMock()
        usage.image_tokens = None
        usage.input_audio_tokens = None
        usage.output_audio_tokens = None
        usage.video_tokens = None
        usage.cached_tokens = None
        result = stub._extract_modality_tokens(usage)
        assert all(v == 0 for v in result.values())


# ---------------------------------------------------------------------------
# answer_test_item with debug_random engine
# ---------------------------------------------------------------------------


class TestAnswerTestItemDebugRandom:
    def test_know_it_all_always_returns_correct_answer(self, stub):
        item = _make_item(choices=["X", "Y", "Z"], answer="Y")
        model = _make_model("debug/know_it_all")
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.answer == "Y"

    def test_know_it_all_matches_on_suffix(self, stub):
        """Any model whose ID ends with 'know_it_all' gets the canonical answer."""
        item = _make_item(choices=["A", "B"], answer="A")
        model = _make_model("provider/know_it_all")
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.answer == "A"
        assert result.is_correct is True

    def test_random_model_picks_from_choices(self, stub):
        item = _make_item(choices=["X", "Y", "Z"])
        model = _make_model("debug/debug_random_model_1")
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.answer in ["X", "Y", "Z"]

    def test_answer_carries_run_id_and_item_id(self, stub):
        item = _make_item(item_id="my-item")
        model = _make_model()
        result = _run(
            stub.answer_test_item(item, "my-run", model, _DEBUG_BACKEND, None)
        )
        assert result.run_id == "my-run"
        assert result.item_id == "my-item"

    def test_model_id_recorded(self, stub):
        item = _make_item()
        model = _make_model("p/special-model")
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.model_id == "p/special-model"

    def test_task_duration_is_non_negative(self, stub):
        item = _make_item()
        model = _make_model()
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.task_duration >= 0.0

    def test_debug_reasoning_is_populated(self, stub):
        item = _make_item()
        model = _make_model()
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert "DEBUG ANSWER" in result.reasoning

    def test_result_has_id(self, stub):
        item = _make_item()
        model = _make_model()
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.id  # uuid4 hex is non-empty

    def test_hardcoded_token_counts(self, stub):
        item = _make_item()
        model = _make_model()
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.input_tokens == 500
        assert result.output_tokens == 100

    def test_zero_costs(self, stub):
        # Debug answers are free; the flat debug-run cost is applied at the
        # run level in runs.py, not per answer.
        item = _make_item()
        model = _make_model()
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.input_cost == 0.0
        assert result.output_cost == 0.0

    def test_know_it_all_is_correct(self, stub):
        """know_it_all always returns the canonical answer — is_correct must be True."""
        item = _make_item(choices=["A", "B", "C"], answer="B")
        model = _make_model("debug/know_it_all")
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.is_correct is True

    def test_random_model_is_correct_when_answer_matches(self, stub):
        """is_correct reflects whether the random pick happened to match."""
        item = _make_item(choices=["only_choice"], answer="only_choice")
        model = _make_model()
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.is_correct is True

    def test_random_model_is_incorrect_when_answer_mismatches(self, stub):
        item = _make_item(choices=["wrong"], answer="correct")
        model = _make_model()
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.is_correct is False

    def test_know_it_all_also_has_hardcoded_tokens_and_zero_cost(self, stub):
        item = _make_item(answer="A")
        model = _make_model("debug/know_it_all")
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.input_tokens == 500
        assert result.output_tokens == 100
        assert result.input_cost == 0.0
        assert result.output_cost == 0.0

    def test_attachment_type_is_text(self, stub):
        """DEBUG_RANDOM answers must be classified as TEXT so modality metrics render."""
        item = _make_item()
        model = _make_model()
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.attachment_type == service_pb2.FileModality.TEXT

    def test_raw_response_matches_answer(self, stub):
        item = _make_item(choices=["A", "B"], answer="A")
        model = _make_model("debug/know_it_all")
        result = _run(stub.answer_test_item(item, "run-1", model, _DEBUG_BACKEND, None))
        assert result.raw_response == result.answer


# ---------------------------------------------------------------------------
# <think> block extraction
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


class TestThinkBlockExtraction:
    """The <think>...</think> block from the first API response should be
    captured as reasoning so the second explicit reasoning call is skipped."""

    def _extract(self, raw):
        m = _THINK_RE.search(raw)
        return m.group(1).strip() if m else None

    def test_extracts_think_content(self):
        raw = "<think>This is my reasoning.</think>True"
        assert self._extract(raw) == "This is my reasoning."

    def test_multiline_think_block(self):
        raw = "<think>\nLine one.\nLine two.\n</think>False"
        assert self._extract(raw) == "Line one.\nLine two."

    def test_no_think_block_returns_none(self):
        raw = "True"
        assert self._extract(raw) is None

    def test_case_insensitive(self):
        raw = "<THINK>Reasoning here.</THINK>A"
        assert self._extract(raw) == "Reasoning here."

    def test_empty_think_block(self):
        raw = "<think></think>B"
        assert self._extract(raw) == ""

    def test_think_block_does_not_bleed_into_answer(self):
        raw = "<think>internal notes</think>   Yes"
        reasoning = self._extract(raw)
        assert reasoning == "internal notes"
        # Verify the answer portion is separate
        after_think = _THINK_RE.sub("", raw).strip()
        assert after_think == "Yes"


# ---------------------------------------------------------------------------
# APIConnectionError handling — verifies pool invalidation + reasoning text
# ---------------------------------------------------------------------------


class _FakeResources:
    """Stand-in for BackendResources whose client raises APIConnectionError."""

    def __init__(self, request_timeout=120.0):
        self.request_timeout = request_timeout
        self.client = mock.MagicMock()
        err = APIConnectionError(request=mock.MagicMock())

        async def _raise(*args, **kwargs):
            raise err

        self.client.chat.completions.create = _raise


class TestAPIConnectionErrorHandling:
    def test_main_inference_records_error_without_invalidating_pool(self, monkeypatch):
        fake = _FakeResources()

        async def _get(_backend):
            return fake

        monkeypatch.setattr(inference_module, "get_backend_resources", _get)

        stub = _StubInference()
        item = _make_item()
        model = _make_model("openai/gpt-test")
        backend = service_pb2.Backend(
            id="real-backend",
            backend_type=service_pb2.BackendType.OPENAI,
            base_url="https://api.example/v1",
        )

        result = _run(stub.answer_test_item(item, "run-1", model, backend, None))

        assert "Connection error" in result.reasoning
        assert result.answer == ""
        assert result.raw_response.startswith("[CONNECTION_ERROR]")

    def test_reasoning_call_records_error_without_invalidating_pool(self, monkeypatch):
        """The reasoning sub-call has its own try/except — verify it sets a
        friendly reasoning string when the main call succeeded but the
        reasoning call hits APIConnectionError, without invalidating the
        shared backend pool (which would cascade-fail concurrent requests).
        """
        usage = mock.MagicMock()
        usage.prompt_tokens = 10
        usage.completion_tokens = 5
        usage.image_tokens = 0
        usage.input_audio_tokens = 0
        usage.output_audio_tokens = 0
        usage.video_tokens = 0
        usage.cached_tokens = 0

        choice = mock.MagicMock()
        choice.message.content = "A"
        main_response = mock.MagicMock()
        main_response.choices = [choice]
        main_response.usage = usage

        err = APIConnectionError(request=mock.MagicMock())
        call_count = {"n": 0}

        async def _create(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return main_response
            raise err

        fake = _FakeResources()
        fake.client.chat.completions.create = _create

        async def _get(_backend):
            return fake

        monkeypatch.setattr(inference_module, "get_backend_resources", _get)

        stub = _StubInference()
        item = _make_item(choices=["A", "B"], answer="A")
        model = _make_model("openai/gpt-test")
        backend = service_pb2.Backend(
            id="real-backend",
            backend_type=service_pb2.BackendType.OPENAI,
            base_url="https://api.example/v1",
        )

        result = _run(
            stub.answer_test_item(
                item, "run-1", model, backend, None, include_reasoning=True
            )
        )

        assert call_count["n"] == 2  # main succeeded, reasoning was attempted
        assert "Reasoning connection error" in result.reasoning
        # Main answer must still be preserved — only the reasoning failed.
        assert result.answer == "A"
        assert result.is_correct is True
        assert result.raw_response == "A"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
