"""Unit tests for BackendsMixin: ACL enforcement, field preservation, api_key redaction."""

import asyncio
import unittest.mock as mock

import grpc
import pytest

from server import service_pb2
from server.objects import backends as backends_module
from server.objects.backends import BackendsMixin


@pytest.fixture(autouse=True)
def invalidate_spy(monkeypatch):
    """Replace the module-level invalidate_backend_resources with a spy.

    Tests that just need ACL behavior ignore the spy; tests that care about
    pool invalidation assert against `invalidate_spy.calls`.
    """
    calls: list[str] = []

    async def _spy(backend_id):
        calls.append(backend_id)

    monkeypatch.setattr(backends_module, "invalidate_backend_resources", _spy)
    _spy.calls = calls  # expose the list as an attribute for assertions
    return _spy


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Aborted(BaseException):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _make_context():
    ctx = mock.AsyncMock()

    async def abort(code, msg):
        raise _Aborted(code, msg)

    ctx.abort = abort
    return ctx


class _StubBackends(BackendsMixin):
    def __init__(self, is_admin=False, owner="user@example.com"):
        self._is_admin = is_admin
        self._owner = owner
        self._created = []
        self._updated = []
        self._deleted = []
        # Fake backend store keyed by id → proto_bytes
        self._store: dict[str, bytes] = {}
        self.db_pool = None

    async def get_request_owner(self, request=None):
        return self._owner

    async def is_user_admin(self, user_id):
        return self._is_admin

    async def _grpc_create(self, id, proto_obj, obj_type):
        self._created.append((id, proto_obj, obj_type))
        self._store[id] = proto_obj.SerializeToString()
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

    async def _grpc_update(self, id, proto_obj, obj_type, obj_owner=None):
        self._updated.append((id, proto_obj, obj_type))
        self._store[id] = proto_obj.SerializeToString()
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

    async def _grpc_delete(self, id, obj_type, obj_owner):
        self._deleted.append((id, obj_type, obj_owner))
        self._store.pop(id, None)
        return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

    async def _grpc_get(self, id, obj_type, obj_owner=None):
        return self._store.get(id)

    async def _grpc_list(self, obj_type, limit=50, obj_owner=None):
        return list(self._store.values())

    async def _list_enabled_backends(self, limit):
        results = []
        for proto_bytes in self._store.values():
            b = service_pb2.Backend()
            b.ParseFromString(proto_bytes)
            if b.enabled:
                results.append(proto_bytes)
        return results[:limit] if limit > 0 else results


# ---------------------------------------------------------------------------
# CreateBackend — ACL
# ---------------------------------------------------------------------------


class TestCreateBackendACL:
    def test_admin_can_create(self):
        stub = _StubBackends(is_admin=True, owner="admin@example.com")
        result = _run(
            stub.CreateBackend(
                request=service_pb2.Backend(
                    name="Local vLLM",
                    backend_type=service_pb2.BackendType.VLLM,
                    base_url="http://localhost:8000/v1",
                    api_key="secret",
                ),
                context=_make_context(),
            )
        )
        assert result.code == service_pb2.ResponseCode.SUCCESS
        assert len(stub._created) == 1

    def test_non_admin_is_denied(self):
        stub = _StubBackends(is_admin=False, owner="user@example.com")
        with pytest.raises(_Aborted) as exc_info:
            _run(
                stub.CreateBackend(
                    request=service_pb2.Backend(name="Sneaky"),
                    context=_make_context(),
                )
            )
        assert exc_info.value.code == grpc.StatusCode.PERMISSION_DENIED
        assert len(stub._created) == 0

    def test_create_sets_id_owner_timestamps(self):
        captured = {}
        stub = _StubBackends(is_admin=True, owner="admin@example.com")

        async def spy_create(id, proto_obj, obj_type):
            captured["id"] = proto_obj.id
            captured["owner"] = proto_obj.owner
            captured["created_at"] = proto_obj.created_at_utc.seconds
            captured["modified_at"] = proto_obj.modified_at_utc.seconds
            stub._store[id] = proto_obj.SerializeToString()
            return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

        stub._grpc_create = spy_create

        _run(
            stub.CreateBackend(
                request=service_pb2.Backend(name="test"),
                context=_make_context(),
            )
        )
        assert captured["id"] != ""
        assert captured["owner"] == "admin@example.com"
        assert captured["created_at"] > 0
        assert captured["modified_at"] > 0


# ---------------------------------------------------------------------------
# DeleteBackend — ACL
# ---------------------------------------------------------------------------


class TestDeleteBackendACL:
    def test_admin_can_delete(self):
        stub = _StubBackends(is_admin=True, owner="admin@example.com")
        # Pre-populate store
        b = service_pb2.Backend(id="b1", owner="admin@example.com")
        stub._store["b1"] = b.SerializeToString()

        result = _run(
            stub.DeleteBackend(
                request=service_pb2.DeleteRequest(id="b1"),
                context=_make_context(),
            )
        )
        assert result.code == service_pb2.ResponseCode.SUCCESS
        assert len(stub._deleted) == 1

    def test_non_admin_is_denied(self):
        stub = _StubBackends(is_admin=False, owner="user@example.com")
        with pytest.raises(_Aborted) as exc_info:
            _run(
                stub.DeleteBackend(
                    request=service_pb2.DeleteRequest(id="b1"),
                    context=_make_context(),
                )
            )
        assert exc_info.value.code == grpc.StatusCode.PERMISSION_DENIED
        assert len(stub._deleted) == 0


# ---------------------------------------------------------------------------
# UpdateBackend — ACL
# ---------------------------------------------------------------------------


class TestUpdateBackendACL:
    def test_non_admin_is_denied(self):
        stub = _StubBackends(is_admin=False, owner="user@example.com")
        with pytest.raises(_Aborted) as exc_info:
            _run(
                stub.UpdateBackend(
                    request=service_pb2.Backend(id="b1", name="New name"),
                    context=_make_context(),
                )
            )
        assert exc_info.value.code == grpc.StatusCode.PERMISSION_DENIED


# ---------------------------------------------------------------------------
# GetBackend — api_key redaction
# ---------------------------------------------------------------------------


class TestGetBackendRedaction:
    def test_api_key_is_redacted(self):
        stub = _StubBackends(is_admin=True, owner="admin@example.com")
        original = service_pb2.Backend(
            id="b1", name="My Backend", api_key="super-secret-key"
        )
        stub._store["b1"] = original.SerializeToString()

        result = _run(
            stub.GetBackend(
                request=service_pb2.GetRequest(id="b1"),
                context=_make_context(),
            )
        )
        assert result.api_key == ""
        assert result.name == "My Backend"

    def test_get_missing_backend_aborts(self):
        stub = _StubBackends()
        with pytest.raises(_Aborted) as exc_info:
            _run(
                stub.GetBackend(
                    request=service_pb2.GetRequest(id="nonexistent"),
                    context=_make_context(),
                )
            )
        assert exc_info.value.code == grpc.StatusCode.NOT_FOUND


# ---------------------------------------------------------------------------
# UpdateBackend — field preservation
# ---------------------------------------------------------------------------


class TestUpdateBackendFieldPreservation:
    def test_preserves_api_key_when_empty_sent(self):
        """Sending empty api_key on update must keep the stored key."""
        stub = _StubBackends(is_admin=True, owner="admin@example.com")
        original = service_pb2.Backend(
            id="b1",
            name="Backend",
            owner="admin@example.com",
            api_key="original-secret",
        )
        original.created_at_utc.GetCurrentTime()
        stub._store["b1"] = original.SerializeToString()

        captured = {}

        async def spy_update(id, proto_obj, obj_type, obj_owner=None):
            captured["api_key"] = proto_obj.api_key
            stub._store[id] = proto_obj.SerializeToString()
            return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

        stub._grpc_update = spy_update

        _run(
            stub.UpdateBackend(
                request=service_pb2.Backend(id="b1", name="New Name", api_key=""),
                context=_make_context(),
            )
        )
        assert captured["api_key"] == "original-secret"

    def test_preserves_owner_and_created_at(self):
        stub = _StubBackends(is_admin=True, owner="admin@example.com")
        original = service_pb2.Backend(
            id="b1",
            name="Backend",
            owner="original-owner@example.com",
            api_key="key",
        )
        original.created_at_utc.GetCurrentTime()
        original_created_at = original.created_at_utc.seconds
        stub._store["b1"] = original.SerializeToString()

        captured = {}

        async def spy_update(id, proto_obj, obj_type, obj_owner=None):
            captured["owner"] = proto_obj.owner
            captured["created_at"] = proto_obj.created_at_utc.seconds
            stub._store[id] = proto_obj.SerializeToString()
            return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

        stub._grpc_update = spy_update

        _run(
            stub.UpdateBackend(
                request=service_pb2.Backend(
                    id="b1", name="Updated", owner="attacker@example.com"
                ),
                context=_make_context(),
            )
        )
        assert captured["owner"] == "original-owner@example.com"
        assert captured["created_at"] == original_created_at

    def test_modified_at_is_refreshed(self):
        stub = _StubBackends(is_admin=True, owner="admin@example.com")
        original = service_pb2.Backend(
            id="b1", name="Backend", owner="admin@example.com", api_key="key"
        )
        original.created_at_utc.GetCurrentTime()
        stub._store["b1"] = original.SerializeToString()

        captured = {}

        async def spy_update(id, proto_obj, obj_type, obj_owner=None):
            captured["modified_at"] = proto_obj.modified_at_utc.seconds
            stub._store[id] = proto_obj.SerializeToString()
            return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

        stub._grpc_update = spy_update

        _run(
            stub.UpdateBackend(
                request=service_pb2.Backend(id="b1", name="Updated"),
                context=_make_context(),
            )
        )
        assert captured["modified_at"] > 0


# ---------------------------------------------------------------------------
# ListBackends — api_key redaction
# ---------------------------------------------------------------------------


class TestListBackendsRedaction:
    def test_all_backends_have_empty_api_key(self):
        stub = _StubBackends(is_admin=True)
        for i in range(3):
            b = service_pb2.Backend(
                id=f"b{i}", name=f"Backend {i}", api_key=f"secret-{i}", enabled=True
            )
            stub._store[f"b{i}"] = b.SerializeToString()

        async def collect():
            results = []
            async for backend in stub.ListBackends(
                request=service_pb2.ListRequest(), context=_make_context()
            ):
                results.append(backend)
            return results

        backends = _run(collect())
        assert len(backends) == 3
        for b in backends:
            assert b.api_key == ""


# ---------------------------------------------------------------------------
# CreateBackend — defaults enabled=True
# ---------------------------------------------------------------------------


class TestCreateBackendDefaults:
    def test_new_backend_is_enabled_by_default(self):
        captured = {}
        stub = _StubBackends(is_admin=True)

        async def spy_create(id, proto_obj, obj_type):
            captured["enabled"] = proto_obj.enabled
            stub._store[id] = proto_obj.SerializeToString()
            return service_pb2.StatusReply(code=service_pb2.ResponseCode.SUCCESS, id=id)

        stub._grpc_create = spy_create

        _run(
            stub.CreateBackend(
                request=service_pb2.Backend(name="test"),
                context=_make_context(),
            )
        )
        assert captured["enabled"] is True


# ---------------------------------------------------------------------------
# ListBackends — enabled/disabled visibility
# ---------------------------------------------------------------------------


class TestListBackendsVisibility:
    def _make_backends(self, stub):
        enabled = service_pb2.Backend(
            id="enabled-1", name="Enabled", api_key="k", enabled=True
        )
        disabled = service_pb2.Backend(
            id="disabled-1", name="Disabled", api_key="k", enabled=False
        )
        stub._store["enabled-1"] = enabled.SerializeToString()
        stub._store["disabled-1"] = disabled.SerializeToString()

    async def _collect(self, stub):
        results = []
        async for b in stub.ListBackends(
            request=service_pb2.ListRequest(), context=_make_context()
        ):
            results.append(b)
        return results

    def test_admin_sees_all_backends(self):
        stub = _StubBackends(is_admin=True)
        self._make_backends(stub)
        backends = _run(self._collect(stub))
        assert len(backends) == 2
        ids = {b.id for b in backends}
        assert "enabled-1" in ids
        assert "disabled-1" in ids

    def test_user_sees_only_enabled_backends(self):
        stub = _StubBackends(is_admin=False)
        self._make_backends(stub)
        backends = _run(self._collect(stub))
        assert len(backends) == 1
        assert backends[0].id == "enabled-1"

    def test_user_sees_no_backends_when_all_disabled(self):
        stub = _StubBackends(is_admin=False)
        disabled = service_pb2.Backend(id="off", name="Off", enabled=False)
        stub._store["off"] = disabled.SerializeToString()
        backends = _run(self._collect(stub))
        assert backends == []


# ---------------------------------------------------------------------------
# Pool invalidation on mutation
# ---------------------------------------------------------------------------


class TestPoolInvalidationOnMutation:
    """The original bug: pool was never invalidated when a backend mutated,
    leaving the cached client serving the now-stale api_key/base_url. These
    tests assert the contract that Update and Delete each trigger one
    invalidate_backend_resources call against the affected backend id.
    """

    def test_update_backend_invalidates_pool(self, invalidate_spy):
        stub = _StubBackends(is_admin=True, owner="admin@example.com")
        original = service_pb2.Backend(
            id="b1", name="Backend", owner="admin@example.com", api_key="key"
        )
        original.created_at_utc.GetCurrentTime()
        stub._store["b1"] = original.SerializeToString()

        _run(
            stub.UpdateBackend(
                request=service_pb2.Backend(id="b1", name="Renamed"),
                context=_make_context(),
            )
        )
        assert invalidate_spy.calls == ["b1"]

    def test_delete_backend_invalidates_pool(self, invalidate_spy):
        stub = _StubBackends(is_admin=True, owner="admin@example.com")
        stub._store["b1"] = service_pb2.Backend(id="b1").SerializeToString()
        _run(
            stub.DeleteBackend(
                request=service_pb2.DeleteRequest(id="b1"),
                context=_make_context(),
            )
        )
        assert invalidate_spy.calls == ["b1"]

    def test_acl_denied_update_does_not_invalidate(self, invalidate_spy):
        """Non-admins are aborted before mutation — pool must not be touched."""
        stub = _StubBackends(is_admin=False, owner="user@example.com")
        with pytest.raises(_Aborted):
            _run(
                stub.UpdateBackend(
                    request=service_pb2.Backend(id="b1"),
                    context=_make_context(),
                )
            )
        assert invalidate_spy.calls == []

    def test_acl_denied_delete_does_not_invalidate(self, invalidate_spy):
        stub = _StubBackends(is_admin=False, owner="user@example.com")
        with pytest.raises(_Aborted):
            _run(
                stub.DeleteBackend(
                    request=service_pb2.DeleteRequest(id="b1"),
                    context=_make_context(),
                )
            )
        assert invalidate_spy.calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
