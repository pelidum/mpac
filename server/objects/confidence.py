import dataclasses
import math
import re

MAX_ANSWER_TOKENS = 32
_LEADING_JUNK = " \t\r\n\"'`*"
_THINK_END = "</think>"


@dataclasses.dataclass
class Token:
    token: str
    logprob: float
    top: list[tuple[str, float]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ConfidenceResult:
    confidence: float | None = None
    choice_probs: dict[str, float] = dataclasses.field(default_factory=dict)


def _norm_choice(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _norm_prefix(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).lstrip(_LEADING_JUNK)


def _completes(prefix: str, choice: str) -> bool:
    if not prefix.startswith(choice):
        return False
    return len(prefix) == len(choice) or not prefix[len(choice)].isalnum()


def _consistent(prefix: str, choice: str) -> bool:
    return bool(prefix) and (choice.startswith(prefix) or _completes(prefix, choice))


def _answer_start(tokens: list[Token]) -> int:
    text = "".join(t.token for t in tokens)
    idx = text.lower().rfind(_THINK_END)
    if idx < 0:
        return 0
    end = idx + len(_THINK_END)
    pos = 0
    for i, t in enumerate(tokens):
        pos += len(t.token)
        if pos >= end:
            return i + 1
    return len(tokens)


def _distribute(mass: dict[str, float], targets: list[str], amount: float) -> None:
    share = amount / len(targets)
    for c in targets:
        mass[c] = mass.get(c, 0.0) + share


def answer_confidence(
    tokens: list[Token] | None, choices: list[str], matched_answer: str
) -> ConfidenceResult:
    if not tokens:
        return ConfidenceResult()

    norm_to_choice: dict[str, str] = {}
    for c in choices:
        n = _norm_choice(c)
        if n and n not in norm_to_choice:
            norm_to_choice[n] = c
    norm_choices = list(norm_to_choice)

    mass: dict[str, float] = {}
    prefix = ""
    p_prefix = 1.0
    completed: str | None = None
    pending: str | None = None
    settled: set[str] = set()

    for tok in tokens[_answer_start(tokens) :][:MAX_ANSWER_TOKENS]:
        n_prefix = _norm_prefix(prefix)
        alternatives = [(t, lp) for t, lp in tok.top if t != tok.token]
        next_prefix = prefix + tok.token
        n_next = _norm_prefix(next_prefix)

        if pending is not None:
            elsewhere = 0.0
            for text, lp in alternatives:
                ext = _norm_prefix(prefix + text)
                targets = [
                    c
                    for c in norm_choices
                    if c != pending and c not in settled and _consistent(ext, c)
                ]
                if targets:
                    amount = p_prefix * math.exp(lp)
                    _distribute(mass, targets, amount)
                    elsewhere += amount
            continues = [
                c
                for c in norm_choices
                if c != pending and c not in settled and _consistent(n_next, c)
            ]
            if continues:
                elsewhere += p_prefix * math.exp(tok.logprob)
            mass[pending] = mass.get(pending, 0.0) + max(0.0, p_prefix - elsewhere)
            if not continues:
                completed = pending
                break
            settled.add(pending)
            pending = None
        else:
            for text, lp in alternatives:
                ext = _norm_prefix(prefix + text)
                if not ext or ext == n_prefix:
                    continue
                targets = [
                    c for c in norm_choices if c not in settled and _consistent(ext, c)
                ]
                if targets:
                    _distribute(mass, targets, p_prefix * math.exp(lp))

        prefix = next_prefix
        p_prefix *= math.exp(tok.logprob)
        if not n_next:
            continue
        live = [c for c in norm_choices if c not in settled and _consistent(n_next, c)]
        if not live:
            break
        done = [c for c in live if _completes(n_next, c)]
        if done:
            best = max(done, key=len)
            if any(len(c) > len(best) and c.startswith(best) for c in live):
                pending = best
                continue
            mass[best] = mass.get(best, 0.0) + p_prefix
            completed = best
            break
    else:
        if pending is not None:
            mass[pending] = mass.get(pending, 0.0) + p_prefix
            completed = pending

    choice_probs = {norm_to_choice[c]: min(1.0, p) for c, p in mass.items()}
    if completed is None or completed != _norm_choice(matched_answer or ""):
        return ConfidenceResult(choice_probs=choice_probs)
    return ConfidenceResult(
        confidence=choice_probs[norm_to_choice[completed]],
        choice_probs=choice_probs,
    )


def apply_to_answer(answer_pb, result: ConfidenceResult) -> None:
    if result.confidence is None:
        answer_pb.ClearField("confidence")
    else:
        answer_pb.confidence = result.confidence
    del answer_pb.choice_probabilities[:]
    for choice, p in sorted(result.choice_probs.items(), key=lambda kv: -kv[1]):
        answer_pb.choice_probabilities.add(choice=choice, probability=p)
