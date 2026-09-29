import math

import pytest

from server import service_pb2
from server.objects.confidence import set_answer_confidence


def _tok(text, p, alts):
    token_pb = service_pb2.TokenLogprob(token=text, logprob=math.log(p))
    token_pb.top_logprobs.add(token=text, logprob=math.log(p))
    for t, q in alts:
        token_pb.top_logprobs.add(token=t, logprob=math.log(q))
    return token_pb


def _score(tokens, choices, answer):
    answer_pb = service_pb2.TestRunAnswer(answer=answer)
    set_answer_confidence(answer_pb, tokens, choices)
    return answer_pb


def _probs(answer_pb):
    return {c.choice: c.probability for c in answer_pb.choice_probabilities}


class TestSetAnswerConfidence:
    def test_no_tokens_has_no_confidence(self):
        assert not _score(None, ["A"], "A").HasField("confidence")
        assert len(_score([], ["A"], "A").choice_probabilities) == 0

    def test_single_token_choices(self):
        tokens = [_tok("Yes", 0.8, [("No", 0.15), ("Maybe", 0.05)])]
        r = _score(tokens, ["Yes", "No"], "Yes")
        assert r.confidence == pytest.approx(0.8)
        assert _probs(r) == pytest.approx({"Yes": 0.8, "No": 0.15})

    def test_case_and_whitespace_insensitive(self):
        tokens = [_tok(" yes", 0.7, [(" no", 0.3)])]
        r = _score(tokens, ["Yes", "No"], "Yes")
        assert r.confidence == pytest.approx(0.7)
        assert _probs(r)["No"] == pytest.approx(0.3)

    def test_multi_token_choice_multiplies_probabilities(self):
        tokens = [
            _tok("Hig", 0.9, [("Low", 0.1)]),
            _tok("h", 0.5, [("hly", 0.5)]),
        ]
        r = _score(tokens, ["High", "Low"], "High")
        assert r.confidence == pytest.approx(0.45)
        assert _probs(r)["Low"] == pytest.approx(0.1)

    def test_shared_prefix_mass_goes_to_divergent_branch(self):
        tokens = [
            _tok("Very", 0.9, [("Unsure", 0.1)]),
            _tok(" likely", 0.6, [(" unlikely", 0.4)]),
        ]
        r = _score(tokens, ["Very likely", "Very unlikely", "Unsure"], "Very likely")
        assert r.confidence == pytest.approx(0.54)
        assert _probs(r)["Very unlikely"] == pytest.approx(0.36)
        assert _probs(r)["Unsure"] == pytest.approx(0.1)

    def test_shorter_choice_that_prefixes_longer_one(self):
        tokens = [
            _tok("Yes", 0.9, [("No", 0.1)]),
            _tok(".", 0.8, [(",", 0.2)]),
        ]
        r = _score(tokens, ["Yes", "Yes, partially", "No"], "Yes")
        assert r.confidence == pytest.approx(0.72)
        assert _probs(r)["Yes, partially"] == pytest.approx(0.18)

    def test_longer_choice_generated_through_shorter_one(self):
        tokens = [
            _tok("Yes", 0.9, [("No", 0.1)]),
            _tok(",", 0.5, [(".", 0.5)]),
            _tok(" partially", 1.0, []),
        ]
        r = _score(tokens, ["Yes", "Yes, partially", "No"], "Yes, partially")
        assert r.confidence == pytest.approx(0.45)
        assert _probs(r)["Yes"] == pytest.approx(0.45)

    def test_answer_ending_at_stream_end_with_pending_choice(self):
        tokens = [_tok("Yes", 0.9, [("No", 0.1)])]
        r = _score(tokens, ["Yes", "Yes, partially", "No"], "Yes")
        assert r.confidence == pytest.approx(0.9)

    def test_trailing_punctuation_ignored(self):
        tokens = [_tok("No", 0.6, [("Yes", 0.4)]), _tok(".", 1.0, [])]
        r = _score(tokens, ["Yes", "No"], "No")
        assert r.confidence == pytest.approx(0.6)

    def test_leading_quotes_and_whitespace_skipped(self):
        tokens = [
            _tok("\n", 1.0, []),
            _tok('"', 1.0, []),
            _tok("B", 0.7, [("A", 0.3)]),
        ]
        r = _score(tokens, ["A", "B"], "B")
        assert r.confidence == pytest.approx(0.7)

    def test_inline_think_block_skipped(self):
        tokens = [
            _tok("<think>", 1.0, []),
            _tok("Probably A", 1.0, []),
            _tok("</think>", 1.0, []),
            _tok("\n\n", 1.0, []),
            _tok("B", 0.6, [("A", 0.4)]),
        ]
        r = _score(tokens, ["A", "B"], "B")
        assert r.confidence == pytest.approx(0.6)

    def test_json_answer_is_unmapped(self):
        tokens = [_tok("{", 0.9, [("A", 0.1)]), _tok('"answer"', 1.0, [])]
        r = _score(tokens, ["A", "B"], "A")
        assert not r.HasField("confidence")
        assert _probs(r) == pytest.approx({"A": 0.1})

    def test_mismatch_with_validated_answer_is_unmapped(self):
        tokens = [_tok("A", 0.9, [("B", 0.1)])]
        r = _score(tokens, ["A", "B"], "B")
        assert not r.HasField("confidence")

    def test_alternate_tokenization_adds_to_same_choice(self):
        tokens = [_tok(" Yes", 0.6, [("Yes", 0.3), (" No", 0.1)])]
        r = _score(tokens, ["Yes", "No"], "Yes")
        assert r.confidence == pytest.approx(0.9)

    def test_choice_probabilities_sorted_by_probability(self):
        tokens = [_tok("B", 0.7, [("A", 0.2)])]
        r = _score(tokens, ["A", "B"], "B")
        assert [c.choice for c in r.choice_probabilities] == ["B", "A"]

    def test_clears_previous_values(self):
        answer_pb = service_pb2.TestRunAnswer(answer="A", confidence=0.5)
        answer_pb.choice_probabilities.add(choice="A", probability=0.5)
        set_answer_confidence(answer_pb, None, ["A"])
        assert not answer_pb.HasField("confidence")
        assert len(answer_pb.choice_probabilities) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
