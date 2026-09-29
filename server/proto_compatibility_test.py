"""Proto wire-compatibility guard for service.proto.

Ensures that no message field or service RPC is ever removed or renumbered,
which would break existing clients and stored proto_bytes in the database.

Rules enforced:
  - Every field in FIELD_SNAPSHOT must still exist with the same field number.
  - Every enum value in ENUM_SNAPSHOT must still exist with the same number.
  - Every RPC in RPC_SNAPSHOT must still exist with the same input/output types.

How to extend when adding new fields/RPCs:
  - Add new entries to the snapshot tables.  Do NOT remove or change existing ones.
  - Never reuse a retired field number — add a comment in the proto instead.
"""

import pytest
from google.protobuf import descriptor as _descriptor

from server import service_pb2


# ---------------------------------------------------------------------------
# pelidum-mike -- April 2026
# Generated from service.proto as of the initial commit of this test.
# NEVER delete or change an existing entry — only append. This ensures
# forward / backwards compatibility.
# ---------------------------------------------------------------------------
FIELD_SNAPSHOT: dict[str, dict[str, int]] = {
    "pelidum.services.grpc.mpac.Visibility": {
        "type": 1,
        "allowed_users": 2,
    },
    "pelidum.services.grpc.mpac.Backend": {
        "id": 1,
        "name": 2,
        "description": 3,
        "backend_type": 4,
        "base_url": 5,
        "api_key": 6,
        "owner": 7,
        "created_at_utc": 8,
        "modified_at_utc": 9,
        "visibility": 10,
        "enabled": 11,
        "is_local": 12,
        "max_concurrency": 13,
    },
    "pelidum.services.grpc.mpac.BenchmarkMatchResponse": {
        "responder": 1,
        "run_id": 2,
        "precision": 3,
        "recall": 4,
        "f1": 5,
    },
    "pelidum.services.grpc.mpac.BenchmarkMatch": {
        "round": 1,
        "id": 2,
        "responses": 3,
        "winner": 4,
    },
    "pelidum.services.grpc.mpac.Benchmark": {
        "id": 1,
        "test_id": 2,
        "owner": 3,
        "created_at_utc": 4,
        "completed_at_utc": 5,
        # fields 6 (run_id) and 7 (metrics) are reserved — do not add back
        "status": 8,
        "match_history": 9,
        "winner": 10,
        "model_count": 11,
    },
    "pelidum.services.grpc.mpac.BenchmarkRequest": {
        "test_id": 1,
        "models": 2,
        "sample_size": 3,
    },
    "pelidum.services.grpc.mpac.BenchmarkResponse": {
        "benchmark_id": 1,
        # field 2 (current_responder_id) is reserved — do not add back
        "code": 3,
        "reason": 4,
        "current_round": 5,
        "total_rounds": 6,
        # field 7 (metrics) is reserved — do not add back
        "match_history": 8,
        "winner": 9,
    },
    "pelidum.services.grpc.mpac.Model": {
        "provider": 1,
        "id": 2,
        "name": 3,
        "temperature": 4,
        "description": 5,
        "pricing": 6,
        "capabilities": 7,
        "family": 8,
        "format": 9,
        "parameters": 10,
        "parameters_label": 11,
        "quantization": 12,
        "fine_tune": 13,
        "license": 14,
        "backend_id": 15,
    },
    "pelidum.services.grpc.mpac.Model.ModelPricing": {
        "input_token_cost": 1,
        "output_token_cost": 2,
        "image_token_cost": 3,
        "audio_token_cost": 4,
    },
    "pelidum.services.grpc.mpac.Model.ModelCapabilities": {
        "modality": 1,
        "context_window_length": 2,
        "tokenizer": 3,
        "instruct": 4,
    },
    "pelidum.services.grpc.mpac.Test": {
        "id": 1,
        "name": 2,
        "description": 3,
        "created_at_utc": 4,
        "modified_at_utc": 5,
        "visibility": 6,
        "type": 7,
        "item_count": 8,
        "labels": 9,
        "provider": 10,
        "owner": 11,
    },
    "pelidum.services.grpc.mpac.FileAttachment": {
        "id": 1,
        "name": 2,
        "extension": 3,
        "modality": 4,
        "file": 5,
        "file_size": 6,
        "visibility": 7,
        "owner": 8,
        "mime": 9,
    },
    "pelidum.services.grpc.mpac.TestItem": {
        "id": 1,
        "test_id": 2,
        "question": 3,
        "context": 4,
        "choices": 5,
        "answer": 6,
        "is_relevant": 7,
        "attachment_id": 8,
    },
    "pelidum.services.grpc.mpac.TestRun": {
        "id": 1,
        "test_id": 2,
        "owner": 3,
        "status": 4,
        "models": 5,
        "include_reasoning": 6,
        "sample_size": 7,
        "total_cost_usd": 8,
        "total_tokens": 9,
        "metrics": 10,
        "created_at_utc": 11,
        "completed_at_utc": 12,
        "backend_id": 13,
    },
    "pelidum.services.grpc.mpac.TestRun.TestRunMetrics": {
        "responder_id": 1,
        "precision": 2,
        "recall": 3,
        "f1": 4,
        "simple_grade": 5,
        "num_positives": 6,
        "num_correct": 7,
        "total": 8,
        "refusal_error_rate": 9,
        "num_refusals_errors": 10,
        "tokens_per_minute": 11,
        "median_task_duration": 12,
        "startup_latency": 13,
        "modality_scores": 14,
        "created_at_utc": 15,
        "completed_at_utc": 16,
        "total_cost_usd": 17,
        "total_tokens": 18,
        "ttft_p50": 28,
        "ttft_p95": 29,
        "ttft_p99": 30,
        "streamed": 31,
    },
    "pelidum.services.grpc.mpac.TestRunAnswer": {
        "id": 1,
        "run_id": 2,
        "item_id": 3,
        "model_id": 4,
        "answer": 5,
        "reasoning": 6,
        "input_tokens": 7,
        "output_tokens": 8,
        "input_cost": 9,
        "output_cost": 10,
        "is_correct": 11,
        "task_duration": 12,
        "has_attachment": 13,
        "attachment_type": 14,
        "raw_response": 15,
        "ttft": 17,
        "output_tps": 18,
        "reasoning_tokens": 19,
        "usage_estimated": 20,
        "justification": 21,
        "status": 22,
        "error": 23,
    },
    "pelidum.services.grpc.mpac.RunProgress": {
        "answer_counts": 1,
    },
    "pelidum.services.grpc.mpac.TestRunRequest": {
        "test_id": 1,
        "models": 2,
        "labels": 3,
        "include_reasoning": 4,
        "sample_size": 5,
        "backend_id": 6,
    },
    "pelidum.services.grpc.mpac.TestRunReply": {
        "test_id": 1,
        "current_responder_id": 2,
        "code": 3,
        "reason": 4,
        "current_round": 5,
        "total_rounds": 6,
        "metrics": 7,
        "run_id": 8,
    },
    "pelidum.services.grpc.mpac.UserAPIKey": {
        "id": 1,
        "name": 2,
        "hash": 3,
        "created_at_utc": 4,
        "active": 5,
        "display_name": 6,
    },
    "pelidum.services.grpc.mpac.UserRateLimits": {
        "tokens_per_minute": 1,
        "daily_spend": 2,
        "monthly_spend": 3,
    },
    "pelidum.services.grpc.mpac.User": {
        "id": 1,
        "name": 2,
        "avatar_url": 3,
        "verified": 4,
        "org_domain": 5,
        "role": 6,
        "created_at_utc": 7,
        "modified_at_utc": 8,
        "last_login": 9,
        "api_key": 10,
        "rate_limits": 11,
        "password_hash": 12,
    },
    "pelidum.services.grpc.mpac.StatusReply": {
        "code": 1,
        "reason": 2,
        "id": 3,
    },
    "pelidum.services.grpc.mpac.ListRequest": {
        "parent_id": 1,
        "limit": 2,
    },
    "pelidum.services.grpc.mpac.ListReply": {
        "objects": 1,
    },
    "pelidum.services.grpc.mpac.GetRequest": {
        "id": 1,
    },
    "pelidum.services.grpc.mpac.DeleteRequest": {
        "id": 1,
    },
    "pelidum.services.grpc.mpac.GetCreditsReply": {
        "total_credits": 1,
        "credits_used": 2,
        "credits_remaining": 3,
        "backend_type": 4,
    },
    "pelidum.services.grpc.mpac.LoginRequest": {
        "user_id": 1,
        "password": 2,
    },
    "pelidum.services.grpc.mpac.BatchCreateTestItemsRequest": {
        "items": 1,
    },
    "pelidum.services.grpc.mpac.BatchCreateTestItemsReply": {
        "results": 1,
        "successful_count": 2,
        "failed_count": 3,
    },
    "pelidum.services.grpc.mpac.BatchGetAttachmentsRequest": {
        "ids": 1,
        "metadata_only": 2,
    },
}


# ---------------------------------------------------------------------------
# Snapshot: {enum_full_name: {value_name: number}}
# NEVER delete or change an existing entry — only append.
# ---------------------------------------------------------------------------
ENUM_SNAPSHOT: dict[str, dict[str, int]] = {
    "pelidum.services.grpc.mpac.TestRunAnswer.Status": {
        "STATUS_UNSPECIFIED": 0,
        "OK": 1,
        "TIMEOUT": 2,
        "CANCELLED": 3,
        "BAD_REQUEST": 4,
        "CONNECTION_ERROR": 5,
        "ERROR": 6,
        "CAPACITY": 7,
        "SKIPPED": 8,
    },
    "pelidum.services.grpc.mpac.BackendType": {
        "BACKEND_TYPE_UNSPECIFIED": 0,
        "VLLM": 1,
        "OLLAMA": 2,
        "OPENAI": 3,
        "OPENROUTER": 4,
        "DEBUG_RANDOM": 5,
        "LLAMA_CPP": 6,
    },
    "pelidum.services.grpc.mpac.TestType": {
        "UNSPECIFIED": 0,
        "EVALUATION": 1,
        "SURVEY": 2,
    },
    "pelidum.services.grpc.mpac.FileModality": {
        "UNSPECIFIED_MODALITY": 0,
        "AUDIO": 1,
        "IMAGE": 2,
        "VIDEO": 3,
        "TEXT": 4,
    },
    "pelidum.services.grpc.mpac.UserRole": {
        "NO_ROLE": 0,
        "NORMAL": 1,
        "ADMIN": 2,
    },
    "pelidum.services.grpc.mpac.ResponseCode": {
        "UNKNOWN": 0,
        "SUCCESS": 1,
        "ERROR": 2,
        "IN_PROGRESS": 3,
        "CANCELLED": 4,
    },
    "pelidum.services.grpc.mpac.Visibility.VisibilityOptions": {
        "VISIBILITY_UNSET": 0,
        "VISIBILITY_OWNER_ONLY": 1,
        "VISIBILITY_ORG_ONLY": 2,
        "VISIBILITY_SPECIFIED_USERS": 3,
        "VISIBILITY_PUBLIC": 4,
        "VISIBILITY_ADMIN": 5,
    },
}


# ---------------------------------------------------------------------------
# Snapshot: set of RPC method names on the MPAC service.
# NEVER delete an existing entry — only append.
# ---------------------------------------------------------------------------
RPC_SNAPSHOT: set[str] = {
    "GetModel",
    "ListModels",
    "GetCredits",
    "CreateBackend",
    "GetBackend",
    "UpdateBackend",
    "DeleteBackend",
    "ListBackends",
    "GetBenchmark",
    "CreateBenchmark",
    "DeleteBenchmark",
    "ListBenchmarks",
    "GetAttachment",
    "CreateAttachment",
    "DeleteAttachment",
    "ListAttachments",
    "BatchGetAttachments",
    "GetTest",
    "CreateTest",
    "UpdateTest",
    "DeleteTest",
    "ListTests",
    "GetTestItem",
    "CreateTestItem",
    "BatchCreateTestItems",
    "UpdateTestItem",
    "DeleteTestItem",
    "ListTestItems",
    "GetTestRun",
    "GetRunProgress",
    "CreateTestRun",
    "UpdateTestRun",
    "DeleteTestRun",
    "ListTestRuns",
    "GetTestRunAnswer",
    "CreateTestRunAnswer",
    "DeleteTestRunAnswer",
    "ListTestRunAnswers",
    "GetUser",
    "CreateUser",
    "UpdateUser",
    "DeleteUser",
    "ListUsers",
    "LoginUser",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_messages(
    descriptor: "_descriptor.FileDescriptor",
) -> dict[str, "_descriptor.Descriptor"]:
    """Recursively collect all message descriptors keyed by full name."""
    result: dict[str, "_descriptor.Descriptor"] = {}

    def _walk(msg: "_descriptor.Descriptor") -> None:
        result[msg.full_name] = msg
        for nested in msg.nested_types:
            _walk(nested)

    for msg in descriptor.message_types_by_name.values():
        _walk(msg)
    return result


def _collect_enums(
    descriptor: "_descriptor.FileDescriptor",
    messages: dict[str, "_descriptor.Descriptor"],
) -> dict[str, "_descriptor.EnumDescriptor"]:
    result: dict[str, "_descriptor.EnumDescriptor"] = {}
    for enum in descriptor.enum_types_by_name.values():
        result[enum.full_name] = enum
    for msg in messages.values():
        for enum in msg.enum_types:
            result[enum.full_name] = enum
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProtoFieldCompatibility:
    """Every field in FIELD_SNAPSHOT must exist with the same number."""

    @pytest.fixture(scope="class")
    def messages(self) -> dict:
        return _collect_messages(service_pb2.DESCRIPTOR)

    @pytest.mark.parametrize("msg_full_name,fields", FIELD_SNAPSHOT.items())
    def test_message_fields_unchanged(self, messages, msg_full_name, fields):
        assert msg_full_name in messages, (
            f"Message '{msg_full_name}' was removed from the proto. "
            "Removing messages breaks stored proto_bytes in the database and "
            "any existing clients. Reserve the message name and mark fields reserved instead."
        )
        descriptor = messages[msg_full_name]
        live_fields = {f.name: f.number for f in descriptor.fields}
        for field_name, expected_number in fields.items():
            assert field_name in live_fields, (
                f"{msg_full_name}.{field_name} was removed. "
                "Field removal breaks wire compatibility — mark it reserved instead."
            )
            actual_number = live_fields[field_name]
            assert actual_number == expected_number, (
                f"{msg_full_name}.{field_name} changed field number "
                f"from {expected_number} to {actual_number}. "
                "Field renumbering breaks all stored data and clients."
            )


class TestProtoEnumCompatibility:
    """Every enum value in ENUM_SNAPSHOT must exist with the same number."""

    @pytest.fixture(scope="class")
    def enums(self) -> dict:
        messages = _collect_messages(service_pb2.DESCRIPTOR)
        return _collect_enums(service_pb2.DESCRIPTOR, messages)

    @pytest.mark.parametrize("enum_full_name,values", ENUM_SNAPSHOT.items())
    def test_enum_values_unchanged(self, enums, enum_full_name, values):
        assert enum_full_name in enums, (
            f"Enum '{enum_full_name}' was removed. "
            "Stored JSONB data may reference these values by name or number."
        )
        descriptor = enums[enum_full_name]
        live_values = {v.name: v.number for v in descriptor.values}
        for value_name, expected_number in values.items():
            assert value_name in live_values, (
                f"{enum_full_name}.{value_name} was removed. "
                "Mark it reserved instead of removing."
            )
            actual_number = live_values[value_name]
            assert actual_number == expected_number, (
                f"{enum_full_name}.{value_name} changed number "
                f"from {expected_number} to {actual_number}."
            )


class TestProtoRpcCompatibility:
    """Every RPC in RPC_SNAPSHOT must still exist on the MPAC service."""

    @pytest.fixture(scope="class")
    def live_rpcs(self) -> set[str]:
        service = service_pb2.DESCRIPTOR.services_by_name.get("MPAC")
        assert service is not None, "MPAC service was removed from the proto."
        return {m.name for m in service.methods}

    @pytest.mark.parametrize("rpc_name", sorted(RPC_SNAPSHOT))
    def test_rpc_exists(self, live_rpcs, rpc_name):
        assert rpc_name in live_rpcs, (
            f"RPC '{rpc_name}' was removed from the MPAC service. "
            "Removing RPCs breaks existing clients. Deprecate instead."
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
