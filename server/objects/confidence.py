import math
import re

from server import service_pb2

MAX_ANSWER_TOKENS = 32
_LEADING_JUNK = " \t\r\n\"'`*"
_THINK_END = "</think>"


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


def _answer_start(tokens: list[service_pb2.TokenLogprob]) -> int:
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


def set_answer_confidence(
    answer_pb: service_pb2.TestRunAnswer,
    tokens: list[service_pb2.TokenLogprob] | None,
    choices: list[str],
) -> None:
    answer_pb.ClearField("confidence")
    del answer_pb.choice_probabilities[:]
    if not tokens:
        return

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
        alternatives = [
            (alt.token, alt.logprob)
            for alt in tok.top_logprobs
            if alt.token != tok.token
        ]
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
    for choice, p in sorted(choice_probs.items(), key=lambda kv: -kv[1]):
        answer_pb.choice_probabilities.add(choice=choice, probability=p)
    if completed is not None and completed == _norm_choice(answer_pb.answer):
        answer_pb.confidence = choice_probs[norm_to_choice[completed]]
