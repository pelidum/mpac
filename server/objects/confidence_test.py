import math

import pytest

from server import service_pb2
from server.objects.confidence import Token, answer_confidence, apply_to_answer


def _alt(text, p, alts):
    top = [(text, math.log(p))] + [(t, math.log(q)) for t, q in alts]
    return Token(text, math.log(p), top)


class TestAnswerConfidence:
    def test_no_tokens_has_no_confidence(self):
        assert answer_confidence(None, ["A"], "A").confidence is None
        assert answer_confidence([], ["A"], "A").choice_probs == {}

    def test_single_token_choices(self):
        tokens = [_alt("Yes", 0.8, [("No", 0.15), ("Maybe", 0.05)])]
        r = answer_confidence(tokens, ["Yes", "No"], "Yes")
        assert r.confidence == pytest.approx(0.8)
        assert r.choice_probs == pytest.approx({"Yes": 0.8, "No": 0.15})

    def test_case_and_whitespace_insensitive(self):
        tokens = [_alt(" yes", 0.7, [(" no", 0.3)])]
        r = answer_confidence(tokens, ["Yes", "No"], "Yes")
        assert r.confidence == pytest.approx(0.7)
        assert r.choice_probs["No"] == pytest.approx(0.3)

    def test_multi_token_choice_multiplies_probabilities(self):
        tokens = [
            _alt("Hig", 0.9, [("Low", 0.1)]),
            _alt("h", 0.5, [("hly", 0.5)]),
        ]
        r = answer_confidence(tokens, ["High", "Low"], "High")
        assert r.confidence == pytest.approx(0.45)
        assert r.choice_probs["Low"] == pytest.approx(0.1)

    def test_shared_prefix_mass_goes_to_divergent_branch(self):
        tokens = [
            _alt("Very", 0.9, [("Unsure", 0.1)]),
            _alt(" likely", 0.6, [(" unlikely", 0.4)]),
        ]
        r = answer_confidence(
            tokens, ["Very likely", "Very unlikely", "Unsure"], "Very likely"
        )
        assert r.confidence == pytest.approx(0.54)
        assert r.choice_probs["Very unlikely"] == pytest.approx(0.36)
        assert r.choice_probs["Unsure"] == pytest.approx(0.1)

    def test_shorter_choice_that_prefixes_longer_one(self):
        tokens = [
            _alt("Yes", 0.9, [("No", 0.1)]),
            _alt(".", 0.8, [(",", 0.2)]),
        ]
        r = answer_confidence(tokens, ["Yes", "Yes, partially", "No"], "Yes")
        assert r.confidence == pytest.approx(0.72)
        assert r.choice_probs["Yes, partially"] == pytest.approx(0.18)

    def test_longer_choice_generated_through_shorter_one(self):
        tokens = [
            _alt("Yes", 0.9, [("No", 0.1)]),
            _alt(",", 0.5, [(".", 0.5)]),
            _alt(" partially", 1.0, []),
        ]
        r = answer_confidence(tokens, ["Yes", "Yes, partially", "No"], "Yes, partially")
        assert r.confidence == pytest.approx(0.45)
        assert r.choice_probs["Yes"] == pytest.approx(0.45)

    def test_answer_ending_at_stream_end_with_pending_choice(self):
        tokens = [_alt("Yes", 0.9, [("No", 0.1)])]
        r = answer_confidence(tokens, ["Yes", "Yes, partially", "No"], "Yes")
        assert r.confidence == pytest.approx(0.9)

    def test_trailing_punctuation_ignored(self):
        tokens = [_alt("No", 0.6, [("Yes", 0.4)]), _alt(".", 1.0, [])]
        r = answer_confidence(tokens, ["Yes", "No"], "No")
        assert r.confidence == pytest.approx(0.6)

    def test_leading_quotes_and_whitespace_skipped(self):
        tokens = [
            _alt("\n", 1.0, []),
            _alt('"', 1.0, []),
            _alt("B", 0.7, [("A", 0.3)]),
        ]
        r = answer_confidence(tokens, ["A", "B"], "B")
        assert r.confidence == pytest.approx(0.7)

    def test_inline_think_block_skipped(self):
        tokens = [
            _alt("<think>", 1.0, []),
            _alt("Probably A", 1.0, []),
            _alt("</think>", 1.0, []),
            _alt("\n\n", 1.0, []),
            _alt("B", 0.6, [("A", 0.4)]),
        ]
        r = answer_confidence(tokens, ["A", "B"], "B")
        assert r.confidence == pytest.approx(0.6)

    def test_json_answer_is_unmapped(self):
        tokens = [_alt("{", 0.9, [("A", 0.1)]), _alt('"answer"', 1.0, [])]
        r = answer_confidence(tokens, ["A", "B"], "A")
        assert r.confidence is None
        assert r.choice_probs == pytest.approx({"A": 0.1})

    def test_mismatch_with_validated_answer_is_unmapped(self):
        tokens = [_alt("A", 0.9, [("B", 0.1)])]
        r = answer_confidence(tokens, ["A", "B"], "B")
        assert r.confidence is None

    def test_alternate_tokenization_adds_to_same_choice(self):
        tokens = [_alt(" Yes", 0.6, [("Yes", 0.3), (" No", 0.1)])]
        r = answer_confidence(tokens, ["Yes", "No"], "Yes")
        assert r.confidence == pytest.approx(0.9)


class TestApplyToAnswer:
    def test_fills_answer_fields_sorted_by_probability(self):
        tokens = [_alt("B", 0.7, [("A", 0.2)])]
        answer_pb = service_pb2.TestRunAnswer()
        apply_to_answer(answer_pb, answer_confidence(tokens, ["A", "B"], "B"))
        assert answer_pb.HasField("confidence")
        assert answer_pb.confidence == pytest.approx(0.7)
        assert [c.choice for c in answer_pb.choice_probabilities] == ["B", "A"]

    def test_no_confidence_leaves_field_unset(self):
        answer_pb = service_pb2.TestRunAnswer(confidence=0.5)
        apply_to_answer(answer_pb, answer_confidence(None, ["A"], "A"))
        assert not answer_pb.HasField("confidence")
        assert len(answer_pb.choice_probabilities) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
