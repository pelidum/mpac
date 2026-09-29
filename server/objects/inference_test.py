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
            "reasoning": 0,
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

    def test_reads_openai_token_details(self, stub):
        usage = mock.MagicMock(
            spec=["prompt_tokens_details", "completion_tokens_details"]
        )
        usage.prompt_tokens_details = mock.MagicMock(
            spec=["cached_tokens", "audio_tokens"], cached_tokens=7, audio_tokens=4
        )
        usage.completion_tokens_details = mock.MagicMock(
            spec=["reasoning_tokens", "audio_tokens"],
            reasoning_tokens=30,
            audio_tokens=2,
        )
        result = stub._extract_modality_tokens(usage)
        assert result["cached"] == 7
        assert result["input_audio"] == 4
        assert result["output_audio"] == 2
        assert result["reasoning"] == 30

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

    def test_debug_answers_have_streaming_timings(self, stub):
        result = _run(
            stub.answer_test_item(_make_item(), "run-1", _make_model(), _DEBUG_BACKEND, None)
        )
        assert result.ttft > 0
        assert result.ttft < result.task_duration
        assert result.output_tps > 0

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


def _chunk(content=None, **delta_extra):
    """A streamed chat.completion.chunk with one choice."""
    delta = mock.MagicMock(spec=["content", "model_extra"])
    delta.content = content
    delta.model_extra = delta_extra
    choice = mock.MagicMock()
    choice.delta = delta
    chunk = mock.MagicMock()
    chunk.choices = [choice]
    chunk.usage = None
    return chunk


def _usage_chunk(usage):
    """The final include_usage chunk: no choices, usage populated."""
    chunk = mock.MagicMock()
    chunk.choices = []
    chunk.usage = usage
    return chunk


def _usage(prompt_tokens, completion_tokens, **details):
    usage = mock.MagicMock(spec=["prompt_tokens", "completion_tokens", "model_extra"])
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.model_extra = {}
    if details:
        usage.completion_tokens_details = mock.MagicMock(spec=["reasoning_tokens"])
        usage.completion_tokens_details.reasoning_tokens = details.get(
            "reasoning_tokens", 0
        )
    return usage


class _FakeStream:
    """Async-iterable stand-in for openai.AsyncStream with optional delays."""

    def __init__(self, chunks, delay=0.0, first_delay=0.0):
        self._chunks = chunks
        self._delay = delay
        self._first_delay = first_delay
        self.closed = False

    async def __aiter__(self):
        for i, c in enumerate(self._chunks):
            await asyncio.sleep(self._first_delay if i == 0 else self._delay)
            yield c

    async def close(self):
        self.closed = True


def _streaming_resources(stream, captured=None):
    fake = _FakeResources()

    async def _create(*args, **kwargs):
        if captured is not None:
            captured.update(kwargs)
        return stream

    fake.client.chat.completions.create = _create
    return fake


_OPENAI_BACKEND = service_pb2.Backend(
    id="real-backend",
    backend_type=service_pb2.BackendType.OPENAI,
    base_url="https://api.example/v1",
)


# ---------------------------------------------------------------------------
# Attachment content parts
# ---------------------------------------------------------------------------


def _attachment(mime, data=b"\x00\x01"):
    return service_pb2.FileAttachment(mime=mime, file=data)


class TestAttachmentContentPart:
    def test_image_uses_image_url(self):
        part = inference_module._attachment_content_part(_attachment("image/png"))
        assert part == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAE="},
        }

    def test_video_uses_video_url(self):
        part = inference_module._attachment_content_part(_attachment("video/mp4"))
        assert part == {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,AAE="},
        }

    @pytest.mark.parametrize(
        "mime, fmt",
        [
            ("audio/wav", "wav"),
            ("audio/x-wav", "wav"),
            ("audio/mpeg", "mp3"),
            ("audio/mp3", "mp3"),
            ("audio/flac", "flac"),
            ("audio/ogg; codecs=opus", "ogg"),
        ],
    )
    def test_audio_uses_input_audio(self, mime, fmt):
        part = inference_module._attachment_content_part(_attachment(mime))
        assert part == {
            "type": "input_audio",
            "input_audio": {"data": "AAE=", "format": fmt},
        }

    def test_non_media_returns_none(self):
        assert inference_module._attachment_content_part(_attachment("text/plain")) is None

    def test_video_attachment_sent_to_backend(self, monkeypatch):
        captured = {}
        stream = _FakeStream([_chunk(content="A"), _usage_chunk(_usage(10, 1))])
        fake = _streaming_resources(stream, captured)

        async def _get(_backend):
            return fake

        class _VideoStub(_StubInference):
            async def GetAttachment(self, request, context):
                return service_pb2.FileAttachment(
                    mime="video/mp4", file=b"\x00\x01", modality=3
                )

        monkeypatch.setattr(inference_module, "get_backend_resources", _get)
        item = _make_item(choices=["A", "B"], answer="A")
        item.attachment_id = "att-1"
        result = _run(
            _VideoStub().answer_test_item(
                item, "run-1", _make_model("openai/gpt-test"), _OPENAI_BACKEND, None
            )
        )
        assert result.attachment_type == service_pb2.FileModality.VIDEO
        media_msg = captured["messages"][-1]
        assert media_msg["content"][0]["type"] == "video_url"
        assert "image_url" not in media_msg["content"][0]


# ---------------------------------------------------------------------------
# Streaming inference: TTFT / output TPS / latency
# ---------------------------------------------------------------------------


class TestStreamingInference:
    def _answer(self, monkeypatch, stream, captured=None, choices=("A", "B")):
        fake = _streaming_resources(stream, captured)

        async def _get(_backend):
            return fake

        monkeypatch.setattr(inference_module, "get_backend_resources", _get)
        item = _make_item(choices=list(choices), answer="A")
        return _run(
            _StubInference().answer_test_item(
                item, "run-1", _make_model("openai/gpt-test"), _OPENAI_BACKEND, None
            )
        )

    def test_requests_streaming_with_usage(self, monkeypatch):
        captured = {}
        stream = _FakeStream([_chunk(content="A"), _usage_chunk(_usage(10, 1))])
        self._answer(monkeypatch, stream, captured)
        assert captured["stream"] is True
        assert captured["stream_options"] == {"include_usage": True}
        assert stream.closed

    def test_ttft_tps_and_latency(self, monkeypatch):
        stream = _FakeStream(
            [_chunk(content="A"), _chunk(content=" is"), _chunk(content=" it")]
            + [_usage_chunk(_usage(20, 5))],
            first_delay=0.2,
            delay=0.05,
        )
        result = self._answer(monkeypatch, stream)
        assert 0.15 < result.ttft < 0.5
        assert result.task_duration >= result.ttft
        # 5 tokens over the post-first-token window of ~0.1s -> ~40 tok/s;
        # prefill (first_delay) must not be in the denominator.
        assert 20 < result.output_tps < 60
        assert result.input_tokens == 20
        assert result.output_tokens == 5
        assert not result.usage_estimated

    def test_single_token_answer_has_no_tps(self, monkeypatch):
        stream = _FakeStream([_chunk(content="A"), _usage_chunk(_usage(10, 1))])
        result = self._answer(monkeypatch, stream)
        assert result.ttft > 0
        assert result.output_tps == 0.0
        assert result.answer == "A"

    def test_reasoning_deltas_count_toward_ttft(self, monkeypatch):
        stream = _FakeStream(
            [
                _chunk(reasoning_content="thinking"),
                _chunk(reasoning_content=" more"),
                _chunk(content="A"),
                _usage_chunk(_usage(10, 3)),
            ],
            first_delay=0.1,
            delay=0.1,
        )
        result = self._answer(monkeypatch, stream)
        assert result.ttft < 0.18  # first reasoning delta, not first content
        assert result.raw_response == "A"  # verbatim content only
        assert result.reasoning == "thinking more"
        assert result.status == service_pb2.TestRunAnswer.OK
        assert result.answer == "A"

    def test_reasoning_only_response_is_not_an_error(self, monkeypatch):
        stream = _FakeStream(
            [_chunk(reasoning="just thinking"), _usage_chunk(_usage(10, 2))]
        )
        result = self._answer(monkeypatch, stream)
        assert result.status == service_pb2.TestRunAnswer.OK
        assert result.reasoning == "just thinking"
        assert result.raw_response == ""
        assert result.answer == ""

    def test_hidden_reasoning_tokens_excluded_from_tps(self, monkeypatch):
        stream = _FakeStream(
            [_chunk(content="A"), _chunk(content="B"), _chunk(content="C")]
            + [_usage_chunk(_usage(10, 103, reasoning_tokens=100))],
            delay=0.05,
        )
        result = self._answer(monkeypatch, stream)
        assert result.reasoning_tokens == 100
        assert result.output_tokens == 103
        # 3 visible tokens over ~0.1s, not 103.
        assert result.output_tps < 60

    def test_missing_usage_is_estimated(self, monkeypatch):
        stream = _FakeStream(
            [_chunk(content="A"), _chunk(content="B"), _chunk(content="C")],
            delay=0.05,
        )
        result = self._answer(monkeypatch, stream)
        assert result.usage_estimated
        assert result.output_tokens == 3
        assert result.input_tokens == 0
        assert result.output_tps > 0

    def test_timeout_mid_stream(self, monkeypatch):
        fake_stream = _FakeStream([_chunk(content="A"), _chunk(content="B")], delay=5)
        fake = _streaming_resources(fake_stream)
        fake.request_timeout = -9.9  # wait_for budget = request_timeout + 10s

        async def _get(_backend):
            return fake

        monkeypatch.setattr(inference_module, "get_backend_resources", _get)
        result = _run(
            _StubInference().answer_test_item(
                _make_item(), "run-1", _make_model("openai/gpt-test"),
                _OPENAI_BACKEND, None,
            )
        )
        assert result.status == service_pb2.TestRunAnswer.TIMEOUT
        assert result.error
        assert result.raw_response == ""
        assert result.reasoning == ""
        assert fake_stream.closed
        assert result.ttft == 0.0


# ---------------------------------------------------------------------------
# Native reasoning: request params, extraction, TPS on summarized reasoning
# ---------------------------------------------------------------------------


_OPENROUTER_BACKEND = service_pb2.Backend(
    id="or-backend",
    backend_type=service_pb2.BackendType.OPENROUTER,
    base_url="https://openrouter.ai/api/v1",
)
_LOCAL_BACKEND = service_pb2.Backend(
    id="local-backend",
    backend_type=service_pb2.BackendType.LLAMA_CPP,
    base_url="http://localhost:1234/v1",
    is_local=True,
)


class TestReasoningRequestParams:
    def test_off_sends_nothing(self):
        for bt in ("openrouter", "openai", "vllm", "llama_cpp", "ollama"):
            assert inference_module._reasoning_request_params(bt, False) == {}

    def test_openrouter_requests_effort(self):
        assert inference_module._reasoning_request_params("openrouter", True) == {
            "extra_body": {"reasoning": {"effort": "medium"}}
        }

    def test_openai_requests_reasoning_effort(self):
        assert inference_module._reasoning_request_params("openai", True) == {
            "reasoning_effort": "medium"
        }

    def test_local_backends_send_nothing(self):
        for bt in ("vllm", "llama_cpp", "ollama"):
            assert inference_module._reasoning_request_params(bt, True) == {}


class TestNativeReasoning:
    def _answer(self, monkeypatch, create, backend, include_reasoning=True):
        fake = _FakeResources()
        fake.client.chat.completions.create = create

        async def _get(_backend):
            return fake

        monkeypatch.setattr(inference_module, "get_backend_resources", _get)
        return _run(
            _StubInference().answer_test_item(
                _make_item(choices=["A", "B"], answer="A"),
                "run-1",
                _make_model("some/model"),
                backend,
                None,
                include_reasoning=include_reasoning,
            )
        )

    def test_openrouter_on_sends_reasoning_param(self, monkeypatch):
        calls = []

        async def _create(*args, **kwargs):
            calls.append(kwargs)
            return _FakeStream(
                [_chunk(reasoning="because"), _chunk(content="A"), _usage_chunk(_usage(10, 3))]
            )

        self._answer(monkeypatch, _create, _OPENROUTER_BACKEND)
        assert calls[0]["extra_body"] == {"reasoning": {"effort": "medium"}}

    def test_off_sends_no_reasoning_param(self, monkeypatch):
        calls = []

        async def _create(*args, **kwargs):
            calls.append(kwargs)
            return _FakeStream([_chunk(content="A"), _usage_chunk(_usage(10, 1))])

        self._answer(monkeypatch, _create, _OPENROUTER_BACKEND, include_reasoning=False)
        assert "extra_body" not in calls[0]
        assert "reasoning_effort" not in calls[0]

    def test_native_reasoning_extracted_and_follow_up_skipped(self, monkeypatch):
        calls = []

        async def _create(*args, **kwargs):
            calls.append(kwargs)
            return _FakeStream(
                [
                    _chunk(reasoning_content="The content is benign."),
                    _chunk(content="A"),
                    _usage_chunk(_usage(10, 8)),
                ]
            )

        result = self._answer(monkeypatch, _create, _LOCAL_BACKEND)
        assert result.reasoning == "The content is benign."
        assert result.justification == ""
        assert result.answer == "A"
        assert len(calls) == 1  # no "explain your answer" follow-up

    def test_reasoning_stored_even_when_toggle_off(self, monkeypatch):
        async def _create(*args, **kwargs):
            return _FakeStream(
                [_chunk(reasoning="thinking"), _chunk(content="A"), _usage_chunk(_usage(10, 3))]
            )

        result = self._answer(
            monkeypatch, _create, _LOCAL_BACKEND, include_reasoning=False
        )
        assert result.reasoning == "thinking"

    def test_rejected_reasoning_param_retries_without(self, monkeypatch):
        from openai import BadRequestError

        calls = []

        async def _create(*args, **kwargs):
            calls.append(kwargs)
            if "reasoning_effort" in kwargs:
                raise BadRequestError(
                    "Unsupported parameter: reasoning_effort",
                    response=mock.MagicMock(status_code=400),
                    body=None,
                )
            if len(calls) == 2:
                return _FakeStream([_chunk(content="A"), _usage_chunk(_usage(10, 1))])
            # Follow-up "explain" call (non-streaming), since no native reasoning.
            follow_up = mock.MagicMock()
            follow_up.choices = [mock.MagicMock()]
            follow_up.choices[0].message.content = "Because A."
            follow_up.usage = _usage(20, 3)
            return follow_up

        result = self._answer(monkeypatch, _create, _OPENAI_BACKEND)
        assert "reasoning_effort" in calls[0]
        assert "reasoning_effort" not in calls[1]
        assert result.answer == "A"
        assert result.status == service_pb2.TestRunAnswer.OK
        assert result.justification == "Because A."
        assert result.reasoning == ""

    def test_failed_request_skips_justification(self, monkeypatch):
        calls = []

        async def _create(*args, **kwargs):
            calls.append(kwargs)
            raise APIConnectionError(request=mock.MagicMock())

        result = self._answer(monkeypatch, _create, _OPENAI_BACKEND)
        assert len(calls) == 1
        assert result.status == service_pb2.TestRunAnswer.CONNECTION_ERROR
        assert result.justification == ""

    def test_inline_think_is_reasoning_and_raw_is_verbatim(self, monkeypatch):
        async def _create(*args, **kwargs):
            return _FakeStream(
                [_chunk(content="<think>hmm</think>"), _chunk(content="A"), _usage_chunk(_usage(10, 5))]
            )

        result = self._answer(monkeypatch, _create, _LOCAL_BACKEND)
        assert result.raw_response == "<think>hmm</think>A"
        assert result.reasoning == "hmm"
        # (Answer matching strips <think>; covered by db_test's _validate_answer.)

    def test_summarized_reasoning_excluded_from_tps(self, monkeypatch):
        """Gemini via OpenRouter: 165 reasoning tokens, a short summary in one
        chunk, then a one-token answer. TPS is undefined, not ~2000 tok/s."""

        async def _create(*args, **kwargs):
            return _FakeStream(
                [
                    _chunk(reasoning="**Analyzing** I think it's fine."),
                    _chunk(content="A"),
                    _usage_chunk(_usage(10, 166, reasoning_tokens=165)),
                ],
                first_delay=0.2,
                delay=0.05,
            )

        result = self._answer(monkeypatch, _create, _OPENROUTER_BACKEND)
        assert result.ttft > 0.15
        assert result.output_tps == 0.0
        assert result.reasoning_tokens == 165

    def test_live_reasoning_counts_toward_tps(self, monkeypatch):
        """Reasoning streamed token-by-token (e.g. qwen3) is part of TPS even
        when the provider reports reasoning_tokens."""
        words = [f"w{i:02d} " for i in range(10)]  # ~4 chars ~= 1 token each

        async def _create(*args, **kwargs):
            return _FakeStream(
                [_chunk(reasoning=w) for w in words]
                + [_chunk(content="A"), _usage_chunk(_usage(10, 11, reasoning_tokens=10))],
                delay=0.02,
            )

        result = self._answer(monkeypatch, _create, _OPENROUTER_BACKEND)
        # 10 tokens over ~0.2s window -> ~50 tok/s
        assert 20 < result.output_tps < 100


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

        assert result.status == service_pb2.TestRunAnswer.CONNECTION_ERROR
        assert "Connection error" in result.error
        assert result.answer == ""
        assert result.raw_response == ""
        assert result.reasoning == ""

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

        err = APIConnectionError(request=mock.MagicMock())
        call_count = {"n": 0}

        async def _create(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _FakeStream([_chunk(content="A"), _usage_chunk(usage)])
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
        assert "Justification connection error" in result.error
        assert result.justification == ""
        assert result.reasoning == ""
        # The primary request succeeded, so the answer's status is OK.
        assert result.status == service_pb2.TestRunAnswer.OK
        # Main answer must still be preserved — only the reasoning failed.
        assert result.answer == "A"
        assert result.is_correct is True
        assert result.raw_response == "A"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
