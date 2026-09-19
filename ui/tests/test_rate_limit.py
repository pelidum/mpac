"""Tests for login rate limiting."""

import time
from unittest import mock

import pytest

from ui.rate_limit import LoginRateLimiter


def test_allows_under_limit():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for _ in range(3):
        assert not limiter.is_blocked("1.2.3.4")
        limiter.record_failure("1.2.3.4")


def test_blocks_at_limit():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for _ in range(3):
        limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4")


def test_different_ips_independent():
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)
    limiter.record_failure("1.1.1.1")
    limiter.record_failure("1.1.1.1")
    assert limiter.is_blocked("1.1.1.1")
    assert not limiter.is_blocked("2.2.2.2")


def test_clear_resets_counter():
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4")
    limiter.clear("1.2.3.4")
    assert not limiter.is_blocked("1.2.3.4")


def test_window_expiry():
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)
    with mock.patch("ui.rate_limit.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        limiter.record_failure("1.2.3.4")
        limiter.record_failure("1.2.3.4")

        mock_time.monotonic.return_value = 1061.0
        assert not limiter.is_blocked("1.2.3.4")


def test_seconds_until_unblocked():
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    remaining = limiter.seconds_until_unblocked("1.2.3.4")
    assert remaining > 0
    assert remaining <= 60


@pytest.mark.asyncio
async def test_login_rate_limit_blocks_after_failures(anon_client, mock_stub):
    import grpc

    error = grpc.RpcError()
    error.code = lambda: grpc.StatusCode.UNAUTHENTICATED
    error.details = lambda: "Invalid credentials"
    mock_stub.LoginUser.side_effect = error

    with mock.patch(
        "ui.rate_limit.login_limiter",
        LoginRateLimiter(max_attempts=2, window_seconds=60),
    ):
        for _ in range(2):
            r = await anon_client.post(
                "/login/password",
                data={"username": "bad", "password": "bad"},
                follow_redirects=False,
            )
            assert r.status_code == 401

        r = await anon_client.post(
            "/login/password",
            data={"username": "bad", "password": "bad"},
            follow_redirects=False,
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
