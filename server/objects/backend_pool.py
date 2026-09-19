"""Per-backend async resource pool: AsyncOpenAI client + concurrency semaphore.

A single `BackendResources` per `backend.id` owns the tuned httpx client and the
semaphore that bounds in-flight requests. Centralizing them here:

  * couples the httpx pool size to the semaphore limit (so the pool is sized to
    exactly the concurrency we permit, no stale or surplus connections);
  * caps keepalive expiry below typical provider idle-close (~20-30s for OpenAI /
    Anthropic), evicting sockets before the remote tears them down — this is the
    fix for the `httpx.ReadError` clusters we were seeing in production;
  * makes invalidate-on-failure atomic — `acquire()` drops the entry and closes
    the httpx pool when an `APIConnectionError` escapes the `async with` block.

Module-scoped registry: matches the previous `_BACKEND_SEMS` lifetime
(process-wide, shared across gRPC servicer instances).
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from absl import logging
from openai import APIConnectionError, AsyncOpenAI

from server import service_pb2

LOCAL_REQUEST_TIMEOUT = 600.0
REMOTE_REQUEST_TIMEOUT = 120.0
LOCAL_CONCURRENCY_DEFAULT = 4
REMOTE_CONCURRENCY_DEFAULT = 20
_REMOTE_KEEPALIVE_EXPIRY = 10.0
_LOCAL_KEEPALIVE_EXPIRY = 30.0
_SEM_ACQUIRE_TIMEOUT_REMOTE = 3600.0  # 1 hour
_SEM_ACQUIRE_TIMEOUT_LOCAL = 14400.0  # 4 hours

_REGISTRY: dict[str, "BackendResources"] = {}
_REGISTRY_LOCK = asyncio.Lock()


def _resolve_concurrency(backend: "service_pb2.Backend") -> int:
    if backend.max_concurrency > 0:
        return backend.max_concurrency
    return LOCAL_CONCURRENCY_DEFAULT if backend.is_local else REMOTE_CONCURRENCY_DEFAULT


def _resolve_request_timeout(backend: "service_pb2.Backend") -> float:
    return LOCAL_REQUEST_TIMEOUT if backend.is_local else REMOTE_REQUEST_TIMEOUT


def _resolve_sem_acquire_timeout(backend: "service_pb2.Backend") -> float:
    return (
        _SEM_ACQUIRE_TIMEOUT_LOCAL if backend.is_local else _SEM_ACQUIRE_TIMEOUT_REMOTE
    )


def _build_httpx_config(
    backend: "service_pb2.Backend", concurrency: int
) -> tuple[httpx.Limits, httpx.Timeout]:
    """Return the (limits, timeout) pair that should be used for `backend`.

    Exposed separately so tests can pin the contract — the production bug was
    that httpx ran with defaults (5s keepalive, no read timeout). If a future
    refactor regresses any of these values, the matching backend_pool_test
    cases will fail.
    """
    if backend.is_local:
        max_conns = concurrency + 2
        keepalive_expiry = _LOCAL_KEEPALIVE_EXPIRY
        read_timeout = LOCAL_REQUEST_TIMEOUT
    else:
        max_conns = concurrency + 5
        keepalive_expiry = _REMOTE_KEEPALIVE_EXPIRY
        read_timeout = REMOTE_REQUEST_TIMEOUT
    limits = httpx.Limits(
        max_connections=max_conns,
        max_keepalive_connections=concurrency,
        keepalive_expiry=keepalive_expiry,
    )
    timeout = httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=5.0)
    return limits, timeout


class BackendResources:
    """Owns the AsyncOpenAI client + semaphore for a single backend.

    Always acquire the client via `async with resources.acquire()` so the
    semaphore is held for the duration of the request and connection-error
    recovery (pool invalidation) happens atomically.
    """

    def __init__(self, backend: "service_pb2.Backend"):
        self.backend_id = backend.id
        self.concurrency = _resolve_concurrency(backend)
        self.request_timeout = _resolve_request_timeout(backend)
        self.sem_acquire_timeout = _resolve_sem_acquire_timeout(backend)
        self.limits, self.timeout = _build_httpx_config(backend, self.concurrency)
        self._httpx_client = httpx.AsyncClient(limits=self.limits, timeout=self.timeout)
        self.client = AsyncOpenAI(
            api_key=backend.api_key or "NOT_A_REAL_API_KEY",
            base_url=backend.base_url,
            http_client=self._httpx_client,
        )
        self.semaphore = asyncio.Semaphore(self.concurrency)
        logging.info(
            f"Created BackendResources for backend {self.backend_id!r}: "
            f"{'local' if backend.is_local else 'remote'}, concurrency={self.concurrency}, "
            f"keepalive_expiry={self.limits.keepalive_expiry}s"
            + (" (explicit)" if backend.max_concurrency > 0 else " (default)")
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client. Safe to call multiple times."""
        try:
            await self._httpx_client.aclose()
        except Exception as e:
            logging.warning(f"BackendResources({self.backend_id}) aclose error: {e}")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[AsyncOpenAI]:
        """Hold a semaphore slot and yield the AsyncOpenAI client.

        Bounds queue depth via `wait_for` so that benchmark dispatch (now
        background-tasked) cannot pile up waiters indefinitely; the resulting
        TimeoutError is handled by the existing block in answer_test_item.

        On `APIConnectionError` escape, the entire pool is invalidated — the
        next request through `get_backend_resources` builds a fresh client.
        """
        try:
            await asyncio.wait_for(
                self.semaphore.acquire(), timeout=self.sem_acquire_timeout
            )
        except asyncio.TimeoutError:
            logging.warning(
                f"BackendResources({self.backend_id}) semaphore acquire timeout"
            )
            raise
        try:
            yield self.client
        except APIConnectionError as e:
            logging.warning(
                f"BackendResources({self.backend_id}) pool invalidated: {e}"
            )
            await invalidate_backend_resources(self.backend_id)
            raise
        finally:
            self.semaphore.release()


async def get_backend_resources(backend: "service_pb2.Backend") -> BackendResources:
    """Return the cached BackendResources for `backend`, lazily building it.

    The lock prevents two concurrent first-callers from each building (and
    leaking) an httpx pool.
    """
    key = backend.id or "__unknown__"
    existing = _REGISTRY.get(key)
    if existing is not None:
        return existing
    async with _REGISTRY_LOCK:
        existing = _REGISTRY.get(key)
        if existing is not None:
            return existing
        resources = BackendResources(backend)
        _REGISTRY[key] = resources
        return resources


async def invalidate_backend_resources(backend_id: str) -> None:
    """Drop the cached resources for `backend_id` and close its httpx pool.

    Called from the error path inside `BackendResources.acquire()` and from
    BackendsMixin on UpdateBackend / DeleteBackend.
    """
    existing = _REGISTRY.pop(backend_id, None)
    if existing is not None:
        await existing.aclose()
