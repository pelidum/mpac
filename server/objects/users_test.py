"""Unit tests for UsersMixin: LoginUser input validation."""

import asyncio
import unittest.mock as mock

import grpc
import pytest
from werkzeug.security import generate_password_hash

from server import service_pb2
from server.objects.users import UsersMixin


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


def _make_pool_with_user(user_pb=None):
    """Return a mock db_pool that yields `user_pb` from a fetchrow query."""
    proto_bytes = user_pb.SerializeToString() if user_pb else None
    conn = mock.AsyncMock()
    conn.fetchrow = mock.AsyncMock(return_value=(proto_bytes,) if proto_bytes else None)
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


class _StubUsers(UsersMixin):
    db_pool = None


class _StubUsersWithSpend(UsersMixin):
    """Stub that also provides _get_current_spend (normally from RateLimitsMixin)."""

    _request_owner = "alice@example.com"
    _is_admin = True

    def __init__(self, db_pool, daily_usd=0.0, monthly_usd=0.0):
        self.db_pool = db_pool
        self._daily_usd = daily_usd
        self._monthly_usd = monthly_usd

    async def get_request_owner(self, request):
        return self._request_owner

    async def is_user_admin(self, user_id):
        return self._is_admin

    async def _get_current_spend(self, user_id):
        return self._daily_usd, self._monthly_usd


# ---------------------------------------------------------------------------
# LoginUser — early input validation (no DB interaction needed)
# ---------------------------------------------------------------------------


class TestLoginUserValidation:
    def test_empty_user_id_aborts_invalid_argument(self):
        stub = _StubUsers()
        request = service_pb2.LoginRequest(user_id="", password="secret")
        with pytest.raises(_Aborted) as exc_info:
            _run(stub.LoginUser(request=request, context=_make_context()))
        assert exc_info.value.code == grpc.StatusCode.INVALID_ARGUMENT

    def test_empty_password_aborts_invalid_argument(self):
        stub = _StubUsers()
        request = service_pb2.LoginRequest(user_id="user@example.com", password="")
        with pytest.raises(_Aborted) as exc_info:
            _run(stub.LoginUser(request=request, context=_make_context()))
        assert exc_info.value.code == grpc.StatusCode.INVALID_ARGUMENT

    def test_oversized_user_id_aborts_invalid_argument(self):
        stub = _StubUsers()
        request = service_pb2.LoginRequest(user_id="a" * 256, password="secret")
        with pytest.raises(_Aborted) as exc_info:
            _run(stub.LoginUser(request=request, context=_make_context()))
        assert exc_info.value.code == grpc.StatusCode.INVALID_ARGUMENT

    def test_oversized_password_aborts_invalid_argument(self):
        stub = _StubUsers()
        request = service_pb2.LoginRequest(
            user_id="user@example.com", password="x" * 256
        )
        with pytest.raises(_Aborted) as exc_info:
            _run(stub.LoginUser(request=request, context=_make_context()))
        assert exc_info.value.code == grpc.StatusCode.INVALID_ARGUMENT

    def test_exactly_255_chars_passes_size_guard(self):
        """Credentials at exactly 255 chars should not trigger the oversized guard
        (the check is `> 255`, not `>= 255`); the abort should be UNAUTHENTICATED
        from the missing-user DB path, not INVALID_ARGUMENT from size check."""
        stub = _StubUsers()
        stub.db_pool = _make_pool_with_user(user_pb=None)  # user not found
        request = service_pb2.LoginRequest(user_id="u" * 255, password="p" * 255)
        with pytest.raises(_Aborted) as exc_info:
            _run(stub.LoginUser(request=request, context=_make_context()))
        assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED

    def test_unknown_user_aborts_unauthenticated(self):
        stub = _StubUsers()
        stub.db_pool = _make_pool_with_user(user_pb=None)
        request = service_pb2.LoginRequest(
            user_id="nobody@example.com", password="pass"
        )
        with pytest.raises(_Aborted) as exc_info:
            _run(stub.LoginUser(request=request, context=_make_context()))
        assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED


# ---------------------------------------------------------------------------
# GetUser — spend populated live
# ---------------------------------------------------------------------------


class TestGetUserSpend:
    def test_spend_fields_populated_from_get_current_spend(self):
        """GetUser must populate user_pb.spend.daily_usd and monthly_usd
        from _get_current_spend so callers no longer need a separate RPC."""
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        pool = _make_pool_with_user(user_pb)
        stub = _StubUsersWithSpend(db_pool=pool, daily_usd=3.50, monthly_usd=42.00)

        result = _run(
            stub.GetUser(
                request=service_pb2.GetRequest(id="alice@example.com"),
                context=mock.AsyncMock(),
            )
        )

        assert abs(result.spend.daily_usd - 3.50) < 1e-5
        assert abs(result.spend.monthly_usd - 42.00) < 1e-5

    def test_spend_is_zero_when_get_current_spend_returns_zeros(self):
        user_pb = service_pb2.User()
        user_pb.id = "bob@example.com"
        pool = _make_pool_with_user(user_pb)
        stub = _StubUsersWithSpend(db_pool=pool, daily_usd=0.0, monthly_usd=0.0)

        result = _run(
            stub.GetUser(
                request=service_pb2.GetRequest(id="bob@example.com"),
                context=mock.AsyncMock(),
            )
        )

        assert result.spend.daily_usd == 0.0
        assert result.spend.monthly_usd == 0.0


# ---------------------------------------------------------------------------
# GetUser — sensitive fields stripped (C2 partial fix)
# ---------------------------------------------------------------------------


class TestGetUserSensitiveFieldsStripped:
    def test_password_hash_is_cleared(self):
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        user_pb.password_hash = "scrypt:32768:8:1$secret"
        pool = _make_pool_with_user(user_pb)
        stub = _StubUsersWithSpend(db_pool=pool)

        result = _run(
            stub.GetUser(
                request=service_pb2.GetRequest(id="alice@example.com"),
                context=mock.AsyncMock(),
            )
        )
        assert result.password_hash == ""

    def test_api_key_hash_is_cleared(self):
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        user_pb.api_key.hash = "sha256:deadbeef"
        pool = _make_pool_with_user(user_pb)
        stub = _StubUsersWithSpend(db_pool=pool)

        result = _run(
            stub.GetUser(
                request=service_pb2.GetRequest(id="alice@example.com"),
                context=mock.AsyncMock(),
            )
        )
        assert result.api_key.hash == ""


# ---------------------------------------------------------------------------
# ListUsers — sensitive fields stripped
# ---------------------------------------------------------------------------


class _StubUsersWithList(UsersMixin):
    _request_owner = "user@example.com"
    _is_admin = False

    def __init__(self, db_pool):
        self.db_pool = db_pool

    async def get_request_owner(self, request):
        return self._request_owner

    async def is_user_admin(self, user_id):
        return self._is_admin

    async def _grpc_list(self, obj_type, obj_owner=None, limit=50):
        if obj_type != "users":
            return []
        pool = self.db_pool
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT proto_bytes FROM users LIMIT $1", limit)
            return [r["proto_bytes"] for r in rows]


def _make_pool_with_users(user_pbs):
    conn = mock.AsyncMock()
    conn.fetch = mock.AsyncMock(
        return_value=[{"proto_bytes": u.SerializeToString()} for u in user_pbs]
    )
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


class TestListUsersSensitiveFields:
    def test_password_hash_stripped_from_list(self):
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        user_pb.password_hash = "scrypt:secret"
        user_pb.api_key.hash = "sha256:key"

        pool = _make_pool_with_users([user_pb])
        stub = _StubUsersWithList(db_pool=pool)
        stub._is_admin = True

        results = _run(self._drain(stub))
        assert len(results) == 1
        assert results[0].password_hash == ""
        assert results[0].api_key.hash == ""

    async def _drain(self, stub):
        results = []
        async for u in stub.ListUsers(
            request=service_pb2.ListRequest(limit=50),
            context=_make_context(),
        ):
            results.append(u)
        return results


# ---------------------------------------------------------------------------
# GetUser — authorization (C2 self-scope)
# ---------------------------------------------------------------------------


class TestGetUserAuthorization:
    def test_non_admin_can_get_own_user(self):
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        pool = _make_pool_with_user(user_pb)
        stub = _StubUsersWithSpend(db_pool=pool)
        stub._request_owner = "alice@example.com"
        stub._is_admin = False

        result = _run(
            stub.GetUser(
                request=service_pb2.GetRequest(id="alice@example.com"),
                context=_make_context(),
            )
        )
        assert result.id == "alice@example.com"

    def test_non_admin_cannot_get_other_user(self):
        user_pb = service_pb2.User()
        user_pb.id = "bob@example.com"
        pool = _make_pool_with_user(user_pb)
        stub = _StubUsersWithSpend(db_pool=pool)
        stub._request_owner = "alice@example.com"
        stub._is_admin = False

        with pytest.raises(_Aborted) as exc_info:
            _run(
                stub.GetUser(
                    request=service_pb2.GetRequest(id="bob@example.com"),
                    context=_make_context(),
                )
            )
        assert exc_info.value.code == grpc.StatusCode.PERMISSION_DENIED

    def test_admin_can_get_any_user(self):
        user_pb = service_pb2.User()
        user_pb.id = "bob@example.com"
        pool = _make_pool_with_user(user_pb)
        stub = _StubUsersWithSpend(db_pool=pool)
        stub._request_owner = "admin@example.com"
        stub._is_admin = True

        result = _run(
            stub.GetUser(
                request=service_pb2.GetRequest(id="bob@example.com"),
                context=_make_context(),
            )
        )
        assert result.id == "bob@example.com"


# ---------------------------------------------------------------------------
# ListUsers — non-admin field stripping (C2)
# ---------------------------------------------------------------------------


class TestListUsersNonAdminStripping:
    def test_non_admin_sees_no_sensitive_fields(self):
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        user_pb.name = "Alice"
        user_pb.password_hash = "scrypt:secret"
        user_pb.api_key.hash = "sha256:key"
        user_pb.api_key.name = "my-key"
        user_pb.rate_limits.daily_spend = 1000
        user_pb.spend.daily_usd = 5.0
        user_pb.last_login.GetCurrentTime()

        pool = _make_pool_with_users([user_pb])
        stub = _StubUsersWithList(db_pool=pool)
        stub._is_admin = False

        results = _run(self._drain(stub))
        assert len(results) == 1
        u = results[0]
        assert u.id == "alice@example.com"
        assert u.name == "Alice"
        assert u.password_hash == ""
        assert not u.HasField("api_key")
        assert u.rate_limits.daily_spend == 0
        assert u.spend.daily_usd == 0.0
        assert u.last_login.seconds == 0

    def test_admin_sees_full_record_minus_hashes(self):
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        user_pb.password_hash = "scrypt:secret"
        user_pb.api_key.hash = "sha256:key"
        user_pb.api_key.name = "my-key"
        user_pb.rate_limits.daily_spend = 1000
        user_pb.spend.daily_usd = 5.0
        user_pb.last_login.GetCurrentTime()

        pool = _make_pool_with_users([user_pb])
        stub = _StubUsersWithList(db_pool=pool)
        stub._is_admin = True

        results = _run(self._drain(stub))
        assert len(results) == 1
        u = results[0]
        assert u.password_hash == ""
        assert u.api_key.hash == ""
        assert u.api_key.name == "my-key"
        assert u.rate_limits.daily_spend == 1000
        assert u.spend.daily_usd == 5.0
        assert u.last_login.seconds > 0

    async def _drain(self, stub):
        results = []
        async for u in stub.ListUsers(
            request=service_pb2.ListRequest(limit=50),
            context=_make_context(),
        ):
            results.append(u)
        return results


# ---------------------------------------------------------------------------
# UpdateUser — credential preservation regression (login bug fix)
# ---------------------------------------------------------------------------


class _StubUsersWithUpdate(UsersMixin):
    """Stub with enough infrastructure for the GetUser → UpdateUser → LoginUser
    round-trip that reproduces the credential-wipe bug."""

    _request_owner = "alice@example.com"
    _is_admin = True

    def __init__(self, stored_user_pb):
        self._stored_bytes = stored_user_pb.SerializeToString()
        self._build_pool()

    def _build_pool(self):
        stub_ref = self

        async def _fetchrow(*args, **kwargs):
            return (stub_ref._stored_bytes,) if stub_ref._stored_bytes else None

        conn = mock.AsyncMock()
        conn.fetchrow = _fetchrow
        pool = mock.MagicMock()
        pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
        self.db_pool = pool

    async def get_request_owner(self, request):
        return self._request_owner

    async def is_user_admin(self, user_id):
        return self._is_admin

    async def _get_current_spend(self, user_id):
        return 0.0, 0.0

    async def _grpc_update(self, id, proto_obj, obj_type):
        self._stored_bytes = proto_obj.SerializeToString()
        return service_pb2.StatusReply()

    def _invalidate_rate_limits_cache(self, user_id):
        pass


class TestUpdateUserPreservesCredentials:
    """Regression: UpdateUser must not wipe credentials that GetUser strips."""

    def test_password_hash_survives_update_then_login(self):
        """Full round-trip: user with password → GetUser (strips hash) →
        UpdateUser (should preserve) → LoginUser (must succeed)."""
        password = "correct-horse-battery-staple"
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        user_pb.name = "Alice"
        user_pb.password_hash = generate_password_hash(password)

        stub = _StubUsersWithUpdate(user_pb)

        got = _run(
            stub.GetUser(
                request=service_pb2.GetRequest(id="alice@example.com"),
                context=mock.AsyncMock(),
            )
        )
        assert got.password_hash == ""

        got.name = "Alice Updated"
        _run(stub.UpdateUser(request=got, context=_make_context()))

        login_result = _run(
            stub.LoginUser(
                request=service_pb2.LoginRequest(
                    user_id="alice@example.com", password=password
                ),
                context=_make_context(),
            )
        )
        assert login_result.id == "alice@example.com"

    def test_api_key_hash_survives_update(self):
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        user_pb.api_key.hash = "sha256:original-key-hash"
        user_pb.api_key.active = True

        stub = _StubUsersWithUpdate(user_pb)

        got = _run(
            stub.GetUser(
                request=service_pb2.GetRequest(id="alice@example.com"),
                context=mock.AsyncMock(),
            )
        )
        assert got.api_key.hash == ""

        got.name = "Updated"
        _run(stub.UpdateUser(request=got, context=_make_context()))

        stored = _run(stub._get_stored_user("alice@example.com"))
        assert stored.api_key.hash == "sha256:original-key-hash"


# ---------------------------------------------------------------------------
# UpdateUser — admin-only field pinning (C-1 fix)
# ---------------------------------------------------------------------------


class TestUpdateUserFieldPinning:
    """Non-admin callers must not be able to change role, rate_limits,
    org_domain, or spend via UpdateUser."""

    def _make_stored_user(self):
        user_pb = service_pb2.User()
        user_pb.id = "alice@example.com"
        user_pb.name = "Alice"
        user_pb.role = service_pb2.UserRole.NORMAL
        user_pb.rate_limits.daily_spend = 1000
        user_pb.rate_limits.monthly_spend = 10000
        user_pb.rate_limits.tokens_per_minute = 50000
        user_pb.org_domain = "example.com"
        user_pb.password_hash = generate_password_hash("password123")
        return user_pb

    def test_non_admin_cannot_escalate_role(self):
        stored = self._make_stored_user()
        stub = _StubUsersWithUpdate(stored)
        stub._request_owner = "alice@example.com"
        stub._is_admin = False

        request = service_pb2.User()
        request.id = "alice@example.com"
        request.name = "Alice Updated"
        request.role = service_pb2.UserRole.ADMIN

        _run(stub.UpdateUser(request=request, context=_make_context()))

        result = _run(stub._get_stored_user("alice@example.com"))
        assert result.role == service_pb2.UserRole.NORMAL

    def test_non_admin_cannot_zero_rate_limits(self):
        stored = self._make_stored_user()
        stub = _StubUsersWithUpdate(stored)
        stub._request_owner = "alice@example.com"
        stub._is_admin = False

        request = service_pb2.User()
        request.id = "alice@example.com"
        request.name = "Alice"
        request.rate_limits.daily_spend = 0
        request.rate_limits.monthly_spend = 0

        _run(stub.UpdateUser(request=request, context=_make_context()))

        result = _run(stub._get_stored_user("alice@example.com"))
        assert result.rate_limits.daily_spend == 1000
        assert result.rate_limits.monthly_spend == 10000

    def test_non_admin_cannot_change_org_domain(self):
        stored = self._make_stored_user()
        stub = _StubUsersWithUpdate(stored)
        stub._request_owner = "alice@example.com"
        stub._is_admin = False

        request = service_pb2.User()
        request.id = "alice@example.com"
        request.name = "Alice"
        request.org_domain = "evil.com"

        _run(stub.UpdateUser(request=request, context=_make_context()))

        result = _run(stub._get_stored_user("alice@example.com"))
        assert result.org_domain == "example.com"

    def test_admin_can_change_role(self):
        stored = self._make_stored_user()
        stub = _StubUsersWithUpdate(stored)
        stub._request_owner = "admin@example.com"
        stub._is_admin = True

        request = service_pb2.User()
        request.id = "alice@example.com"
        request.name = "Alice"
        request.role = service_pb2.UserRole.ADMIN

        _run(stub.UpdateUser(request=request, context=_make_context()))

        result = _run(stub._get_stored_user("alice@example.com"))
        assert result.role == service_pb2.UserRole.ADMIN

    def test_non_admin_can_update_allowed_fields(self):
        stored = self._make_stored_user()
        stub = _StubUsersWithUpdate(stored)
        stub._request_owner = "alice@example.com"
        stub._is_admin = False

        request = service_pb2.User()
        request.id = "alice@example.com"
        request.name = "Alice New Name"
        request.avatar_url = "https://example.com/avatar.png"

        _run(stub.UpdateUser(request=request, context=_make_context()))

        result = _run(stub._get_stored_user("alice@example.com"))
        assert result.name == "Alice New Name"
        assert result.avatar_url == "https://example.com/avatar.png"
        assert result.role == service_pb2.UserRole.NORMAL


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
