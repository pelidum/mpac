"""Unit tests for backend_pool: BackendResources lifecycle and recovery."""

import asyncio
import unittest.mock as mock

import pytest
from openai import APIConnectionError

from server import service_pb2
from server.objects import backend_pool


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty registry; close any leftover resources."""

    async def _drain():
        for key in list(backend_pool._REGISTRY):
            await backend_pool.invalidate_backend_resources(key)

    _run(_drain())
    yield
    _run(_drain())


def _make_backend(
    backend_id="b1", is_local=False, max_concurrency=0, base_url="https://example/v1"
):
    return service_pb2.Backend(
        id=backend_id,
        base_url=base_url,
        api_key="dummy",
        is_local=is_local,
        max_concurrency=max_concurrency,
    )


# ---------------------------------------------------------------------------
# Concurrency resolution
# ---------------------------------------------------------------------------


class TestConcurrencyResolution:
    def test_explicit_max_concurrency_wins(self):
        b = _make_backend(max_concurrency=7)
        resources = _run(backend_pool.get_backend_resources(b))
        assert resources.concurrency == 7

    def test_local_default(self):
        b = _make_backend(is_local=True, max_concurrency=0)
        resources = _run(backend_pool.get_backend_resources(b))
        assert resources.concurrency == backend_pool.LOCAL_CONCURRENCY_DEFAULT

    def test_remote_default(self):
        b = _make_backend(is_local=False, max_concurrency=0)
        resources = _run(backend_pool.get_backend_resources(b))
        assert resources.concurrency == backend_pool.REMOTE_CONCURRENCY_DEFAULT


# ---------------------------------------------------------------------------
# request_timeout resolution
# ---------------------------------------------------------------------------


class TestRequestTimeout:
    def test_local_request_timeout(self):
        b = _make_backend(is_local=True)
        resources = _run(backend_pool.get_backend_resources(b))
        assert resources.request_timeout == backend_pool.LOCAL_REQUEST_TIMEOUT

    def test_remote_request_timeout(self):
        b = _make_backend(is_local=False)
        resources = _run(backend_pool.get_backend_resources(b))
        assert resources.request_timeout == backend_pool.REMOTE_REQUEST_TIMEOUT


# ---------------------------------------------------------------------------
# Lazy creation + identity
# ---------------------------------------------------------------------------


class TestRegistryIdentity:
    def test_same_backend_returns_same_instance(self):
        b = _make_backend()
        r1 = _run(backend_pool.get_backend_resources(b))
        r2 = _run(backend_pool.get_backend_resources(b))
        assert r1 is r2

    def test_concurrent_first_callers_get_same_instance(self):
        """The asyncio.Lock must prevent two coroutines from each building."""
        b = _make_backend()

        async def race():
            r1_task = asyncio.create_task(backend_pool.get_backend_resources(b))
            r2_task = asyncio.create_task(backend_pool.get_backend_resources(b))
            return await asyncio.gather(r1_task, r2_task)

        r1, r2 = _run(race())
        assert r1 is r2
        assert len(backend_pool._REGISTRY) == 1


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


class TestInvalidate:
    def test_invalidate_drops_entry(self):
        b = _make_backend()
        r1 = _run(backend_pool.get_backend_resources(b))
        _run(backend_pool.invalidate_backend_resources(b.id))
        assert b.id not in backend_pool._REGISTRY
        r2 = _run(backend_pool.get_backend_resources(b))
        assert r1 is not r2

    def test_invalidate_closes_httpx_client(self):
        b = _make_backend()
        r1 = _run(backend_pool.get_backend_resources(b))
        _run(backend_pool.invalidate_backend_resources(b.id))
        assert r1._httpx_client.is_closed

    def test_invalidate_missing_backend_is_noop(self):
        _run(backend_pool.invalidate_backend_resources("never_existed"))


# ---------------------------------------------------------------------------
# acquire() semantics
# ---------------------------------------------------------------------------


class TestAcquire:
    def test_acquire_yields_client(self):
        b = _make_backend()
        resources = _run(backend_pool.get_backend_resources(b))

        async def use():
            async with resources.acquire() as client:
                return client

        client = _run(use())
        assert client is resources.client

    def test_acquire_releases_on_normal_exit(self):
        b = _make_backend(max_concurrency=1)
        resources = _run(backend_pool.get_backend_resources(b))

        async def use():
            async with resources.acquire():
                pass

        _run(use())

        # Slot was released — subsequent acquire must not block.
        async def reacquire():
            async with resources.acquire():
                return True

        assert _run(reacquire()) is True

    def test_acquire_releases_on_exception(self):
        b = _make_backend(max_concurrency=1)
        resources = _run(backend_pool.get_backend_resources(b))

        async def use_and_raise():
            async with resources.acquire():
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            _run(use_and_raise())

        # If the slot leaked, acquire below would hang. We assert via wait_for.
        async def reacquire():
            async with resources.acquire():
                return True

        assert _run(asyncio.wait_for(reacquire(), timeout=2.0)) is True

    def test_acquire_invalidates_on_api_connection_error(self):
        b = _make_backend()
        r1 = _run(backend_pool.get_backend_resources(b))
        err = APIConnectionError(request=mock.MagicMock())

        async def use_and_raise():
            async with r1.acquire():
                raise err

        with pytest.raises(APIConnectionError):
            _run(use_and_raise())

        assert b.id not in backend_pool._REGISTRY
        r2 = _run(backend_pool.get_backend_resources(b))
        assert r1 is not r2

    def test_acquire_semaphore_timeout(self):
        """When all slots are held and a new acquirer waits past the timeout,
        the wait_for inside acquire() must surface TimeoutError."""
        b = _make_backend(max_concurrency=1)
        resources = _run(backend_pool.get_backend_resources(b))
        # Shorten the gate to keep the test fast — the wait_for branch is what
        # matters, not the literal wall-clock timeout.
        resources.sem_acquire_timeout = 0.05

        async def hold_then_starve():
            holder_release = asyncio.Event()

            async def holder():
                async with resources.acquire():
                    await holder_release.wait()

            holder_task = asyncio.create_task(holder())
            # Give the holder a tick to claim the slot.
            await asyncio.sleep(0.01)
            try:
                async with resources.acquire():
                    pass
            finally:
                holder_release.set()
                await holder_task

        with pytest.raises(asyncio.TimeoutError):
            _run(hold_then_starve())


# ---------------------------------------------------------------------------
# Concurrency enforcement — the contract that protects upstream backends
# ---------------------------------------------------------------------------


class TestConcurrencyEnforcement:
    """The original production bug stemmed from no test ever asserting that
    the semaphore actually caps in-flight requests. These tests do that."""

    def test_max_concurrency_caps_in_flight(self):
        b = _make_backend(max_concurrency=3)
        resources = _run(backend_pool.get_backend_resources(b))

        in_flight = 0
        peak = 0
        release_gates: list[asyncio.Event] = []

        async def hold_slot(gate: asyncio.Event):
            nonlocal in_flight, peak
            async with resources.acquire():
                in_flight += 1
                peak = max(peak, in_flight)
                await gate.wait()
                in_flight -= 1

        async def drive():
            # 6 tasks competing for 3 slots: 3 must wait.
            tasks = []
            for _ in range(6):
                gate = asyncio.Event()
                release_gates.append(gate)
                tasks.append(asyncio.create_task(hold_slot(gate)))
            # Let the first wave claim slots and the rest queue.
            await asyncio.sleep(0.05)
            assert peak == 3, f"peak={peak}; semaphore allowed too many"
            # Drain.
            for gate in release_gates:
                gate.set()
            await asyncio.gather(*tasks)

        _run(drive())
        assert peak == 3
        assert in_flight == 0

    def test_invalidate_during_in_flight_does_not_leak(self):
        """If the pool is invalidated mid-flight, in-flight holders must still
        release cleanly. The new resources object is a different instance."""
        b = _make_backend(max_concurrency=1)
        r1 = _run(backend_pool.get_backend_resources(b))

        async def drive():
            held = asyncio.Event()
            released = asyncio.Event()

            async def holder():
                async with r1.acquire():
                    held.set()
                    await released.wait()

            holder_task = asyncio.create_task(holder())
            await held.wait()
            # Invalidate while a slot is held.
            await backend_pool.invalidate_backend_resources(b.id)
            assert b.id not in backend_pool._REGISTRY
            released.set()
            await holder_task
            # A fresh resources is built on next request, with its own pool.
            r2 = await backend_pool.get_backend_resources(b)
            assert r2 is not r1
            assert not r2._httpx_client.is_closed

        _run(drive())


# ---------------------------------------------------------------------------
# httpx configuration — the actual production fix
# ---------------------------------------------------------------------------


class TestHttpxConfiguration:
    """The production bug was the default httpx pool (5s keepalive, no read
    timeout). These tests pin the explicit config so a future refactor that
    drops it will fail loudly here, not silently in production."""

    def test_remote_pool_size_matches_concurrency(self):
        b = _make_backend(max_concurrency=10, is_local=False)
        resources = _run(backend_pool.get_backend_resources(b))
        limits = resources.limits
        assert limits.max_keepalive_connections == 10
        # max_connections must give headroom beyond the semaphore so a brief
        # slot handoff doesn't stall on the pool.
        assert limits.max_connections > 10

    def test_remote_keepalive_below_provider_idle_close(self):
        """OpenAI / Anthropic close idle sockets around 20-30s. Our expiry
        must be lower so we evict first — this is the fix for the ReadError
        clusters."""
        b = _make_backend(is_local=False)
        resources = _run(backend_pool.get_backend_resources(b))
        keepalive = resources.limits.keepalive_expiry
        assert keepalive is not None
        assert keepalive < 20.0

    def test_read_timeout_matches_request_timeout(self):
        """The httpx read timeout must align with the configured per-request
        timeout — otherwise we'd see TimeoutError before the upstream finishes."""
        b = _make_backend(is_local=False)
        resources = _run(backend_pool.get_backend_resources(b))
        assert resources.timeout.read == resources.request_timeout

    def test_local_keepalive_longer_than_remote(self):
        """Loopback is stable; local backends keep connections warm longer."""
        remote = _run(
            backend_pool.get_backend_resources(_make_backend("r", is_local=False))
        )
        local = _run(
            backend_pool.get_backend_resources(_make_backend("l", is_local=True))
        )
        assert local.limits.keepalive_expiry > remote.limits.keepalive_expiry


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
