"""Status of a TestRunAnswer, with a fallback for answers written before the
`status` field existed.

Older answers encoded a failed request as a "[TAG] message" prefix in
`raw_response` and put a human-readable message in `reasoning`. Readers should
go through `answer_status` / `normalize_answer` rather than parsing those
fields, so old and new answers look the same.
"""

from server import service_pb2

Status = service_pb2.TestRunAnswer.Status

_LEGACY_TAGS = {
    "[TIMEOUT]": Status.TIMEOUT,
    "[CANCELLED]": Status.CANCELLED,
    "[BAD_REQUEST]": Status.BAD_REQUEST,
    "[CONNECTION_ERROR]": Status.CONNECTION_ERROR,
    "[ERROR]": Status.ERROR,
    "[CAPACITY]": Status.CAPACITY,
    "[SKIPPED]": Status.SKIPPED,
}


def _legacy_tag(answer_pb) -> str | None:
    for tag in _LEGACY_TAGS:
        if answer_pb.raw_response.startswith(tag):
            return tag
    return None


def answer_status(answer_pb) -> int:
    """The answer's Status, derived from the legacy prefix when unset."""
    if answer_pb.status != Status.STATUS_UNSPECIFIED:
        return answer_pb.status
    tag = _legacy_tag(answer_pb)
    return _LEGACY_TAGS[tag] if tag else Status.OK


def is_request_success(answer_pb) -> bool:
    return answer_status(answer_pb) == Status.OK


def normalize_answer(answer_pb) -> service_pb2.TestRunAnswer:
    """Return a copy with `status` set and legacy error encoding migrated.

    For a legacy failed answer, the error text moves to `error` and
    `raw_response` / `reasoning` are cleared, since neither held model output.
    New answers are returned as-is (copied).
    """
    normalized = service_pb2.TestRunAnswer()
    normalized.CopyFrom(answer_pb)
    if normalized.status != Status.STATUS_UNSPECIFIED:
        return normalized
    tag = _legacy_tag(normalized)
    if tag is None:
        normalized.status = Status.OK
        return normalized
    normalized.status = _LEGACY_TAGS[tag]
    normalized.error = (
        normalized.reasoning or normalized.raw_response[len(tag) :].strip()
    )
    normalized.reasoning = ""
    normalized.raw_response = ""
    return normalized
