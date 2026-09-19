"""Unit tests for ModelsMixin: model listing, lookup, and credits."""

import asyncio
import unittest.mock as mock

import grpc
import pytest

from server import service_pb2
from server.objects.models import ModelsMixin


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


_ctx = mock.AsyncMock()


class _StubModels(ModelsMixin):
    """Test stub: drives ListModels via _get_enabled_backends + _list_models_for_backend,
    and GetCredits via _grpc_get."""

    def __init__(
        self,
        model_dicts=None,
        backend_type="DEBUG_RANDOM",
        backend_api_key="test",
        backend_base_url="http://localhost",
    ):
        self._model_dicts = model_dicts or []
        self._backend_type_name = backend_type
        self._backend_api_key = backend_api_key
        self._backend_base_url = backend_base_url
        self.db_pool = None

    def _make_backend(self):
        b = service_pb2.Backend()
        b.id = "stub-backend"
        b.api_key = self._backend_api_key
        b.base_url = self._backend_base_url
        b.backend_type = service_pb2.BackendType.Value(self._backend_type_name)
        b.enabled = True
        return b

    async def _get_enabled_backends(self):
        return [self._make_backend()]

    async def _list_models_for_backend(self, backend, context):
        backend_type = service_pb2.BackendType.Name(backend.backend_type).lower()
        for model_dict in self._model_dicts:
            yield self._model_dict_to_pb(model_dict, backend_type)

    async def _grpc_get(self, id, obj_type, obj_owner=None):
        if obj_type == "backends":
            return self._make_backend().SerializeToString()
        return None


async def _collect_models(stub):
    results = []
    async for m in stub.ListModels(request=service_pb2.ListRequest(), context=_ctx):
        results.append(m)
    return results


# ---------------------------------------------------------------------------
# ListModels
# ---------------------------------------------------------------------------


class TestListModels:
    def test_empty_list(self):
        stub = _StubModels(model_dicts=[])
        assert _run(_collect_models(stub)) == []

    def test_yields_ids_in_order(self):
        stub = _StubModels(
            model_dicts=[
                {"id": "p/model-a", "name": "Model A"},
                {"id": "p/model-b", "name": "Model B"},
            ]
        )
        results = _run(_collect_models(stub))
        assert [m.id for m in results] == ["p/model-a", "p/model-b"]

    def test_provider_extracted_from_id(self):
        stub = _StubModels(model_dicts=[{"id": "openai/gpt-4", "name": "GPT-4"}])
        results = _run(_collect_models(stub))
        assert results[0].provider == "openai"

    def test_no_slash_in_id_leaves_provider_empty(self):
        stub = _StubModels(model_dicts=[{"id": "standalone", "name": "Standalone"}])
        results = _run(_collect_models(stub))
        assert results[0].provider == ""
        assert results[0].id == "standalone"

    def test_default_context_window_is_8192(self):
        stub = _StubModels(model_dicts=[{"id": "p/m", "name": "m"}])
        results = _run(_collect_models(stub))
        assert results[0].capabilities.context_window_length == 8192

    def test_context_window_from_model_dict(self):
        stub = _StubModels(
            model_dicts=[{"id": "p/m", "name": "m", "context_length": 128000}]
        )
        results = _run(_collect_models(stub))
        assert results[0].capabilities.context_window_length == 128000

    def test_default_modality_is_text_to_text(self):
        stub = _StubModels(model_dicts=[{"id": "p/m", "name": "m"}])
        results = _run(_collect_models(stub))
        assert results[0].capabilities.modality == "text-to-text"

    def test_description_mapped(self):
        stub = _StubModels(
            model_dicts=[{"id": "p/m", "name": "m", "description": "A model"}]
        )
        results = _run(_collect_models(stub))
        assert results[0].description == "A model"

    def test_name_mapped(self):
        stub = _StubModels(model_dicts=[{"id": "p/m", "name": "My Model"}])
        results = _run(_collect_models(stub))
        assert results[0].name == "My Model"

    def test_no_models_when_no_enabled_backends(self):
        stub = _StubModels(model_dicts=[{"id": "p/m", "name": "m"}])

        async def no_backends():
            return []

        stub._get_enabled_backends = no_backends
        assert _run(_collect_models(stub)) == []


# ---------------------------------------------------------------------------
# GetModel
# ---------------------------------------------------------------------------


class TestGetModel:
    def test_found_by_id(self):
        stub = _StubModels(
            model_dicts=[
                {"id": "p/alpha", "name": "Alpha"},
                {"id": "p/beta", "name": "Beta"},
            ]
        )
        result = _run(
            stub.GetModel(
                request=service_pb2.GetRequest(id="p/beta"),
                context=_ctx,
            )
        )
        assert result.id == "p/beta"
        assert result.name == "Beta"

    def test_not_found_aborts_not_found(self):
        class _Aborted(BaseException):
            def __init__(self, code, message):
                self.code = code
                super().__init__(message)

        ctx = mock.AsyncMock()

        async def abort(code, msg):
            raise _Aborted(code, msg)

        ctx.abort = abort

        stub = _StubModels(model_dicts=[{"id": "p/alpha", "name": "Alpha"}])
        with pytest.raises(_Aborted) as exc_info:
            _run(
                stub.GetModel(
                    request=service_pb2.GetRequest(id="nonexistent"),
                    context=ctx,
                )
            )
        assert exc_info.value.code == grpc.StatusCode.NOT_FOUND

    def test_returns_first_match(self):
        stub = _StubModels(
            model_dicts=[
                {"id": "p/target", "name": "First"},
                {"id": "p/target", "name": "Second"},
            ]
        )
        result = _run(
            stub.GetModel(
                request=service_pb2.GetRequest(id="p/target"),
                context=_ctx,
            )
        )
        assert result.name == "First"


# ---------------------------------------------------------------------------
# GetCredits
# ---------------------------------------------------------------------------


class TestGetCredits:
    def test_non_openrouter_returns_zero_credits(self):
        for engine in ["VLLM", "OLLAMA", "DEBUG_RANDOM", "LLAMA_CPP"]:
            stub = _StubModels(backend_type=engine)
            result = _run(
                stub.GetCredits(
                    request=service_pb2.GetRequest(id="stub-backend"),
                    context=_ctx,
                )
            )
            assert result.total_credits == 0.0
            assert result.credits_remaining == 0.0

    def test_backend_type_reflected(self):
        for engine in ["VLLM", "OLLAMA", "DEBUG_RANDOM", "LLAMA_CPP"]:
            stub = _StubModels(backend_type=engine)
            result = _run(
                stub.GetCredits(
                    request=service_pb2.GetRequest(id="stub-backend"),
                    context=_ctx,
                )
            )
            assert result.backend_type == engine.lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
