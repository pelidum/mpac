"""Unit tests for answer_status: status derivation and legacy normalization."""

import pytest

from server import service_pb2
from server.objects.answer_status import (
    Status,
    answer_status,
    is_request_success,
    normalize_answer,
)


def _answer(**kwargs):
    return service_pb2.TestRunAnswer(**kwargs)


class TestAnswerStatus:
    def test_explicit_status_wins(self):
        a = _answer(status=Status.TIMEOUT, raw_response="[ERROR] ignored")
        assert answer_status(a) == Status.TIMEOUT

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("[TIMEOUT] Inference request timed out", Status.TIMEOUT),
            ("[CANCELLED] Request cancelled by user", Status.CANCELLED),
            ("[BAD_REQUEST] nope", Status.BAD_REQUEST),
            ("[CONNECTION_ERROR] refused", Status.CONNECTION_ERROR),
            ("[ERROR] boom", Status.ERROR),
            ("[CAPACITY] waited too long", Status.CAPACITY),
            ("[SKIPPED] breaker open", Status.SKIPPED),
        ],
    )
    def test_legacy_prefix(self, raw, expected):
        assert answer_status(_answer(raw_response=raw)) == expected
        assert not is_request_success(_answer(raw_response=raw))

    def test_legacy_success(self):
        a = _answer(raw_response="<think>hmm</think>A", answer="A")
        assert answer_status(a) == Status.OK
        assert is_request_success(a)

    def test_legacy_empty_answer_is_ok(self):
        # Pre-status answers that returned no choices had nothing in raw_response.
        assert answer_status(_answer()) == Status.OK


class TestNormalizeAnswer:
    def test_legacy_error_moves_to_error_field(self):
        a = _answer(
            raw_response="[TIMEOUT] Inference request timed out",
            reasoning="Request timeout - inference took too long",
        )
        n = normalize_answer(a)
        assert n.status == Status.TIMEOUT
        assert n.error == "Request timeout - inference took too long"
        assert n.reasoning == ""
        assert n.raw_response == ""
        # Input is not mutated.
        assert a.raw_response.startswith("[TIMEOUT]")

    def test_legacy_error_without_reasoning_uses_prefix_message(self):
        n = normalize_answer(_answer(raw_response="[SKIPPED] breaker open"))
        assert n.error == "breaker open"

    def test_legacy_success_keeps_fields(self):
        a = _answer(raw_response="<think>x</think>A", reasoning="x", answer="A")
        n = normalize_answer(a)
        assert n.status == Status.OK
        assert n.reasoning == "x"
        assert n.raw_response == "<think>x</think>A"

    def test_new_answer_unchanged(self):
        a = _answer(status=Status.ERROR, error="boom", raw_response="")
        assert normalize_answer(a) == a


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
