import asyncio
import traceback
from datetime import datetime, timezone
from typing import Any

from absl import logging

from server import service_pb2

_REAPER_INTERVAL = 300  # 5 minutes
ACTIVE_RUN_STREAMS_LOCK = asyncio.Lock()
ACTIVE_RUN_STREAMS: dict[str, tuple[Any, Any]] = {}
ACTIVE_RUNS_BY_ID_LOCK = asyncio.Lock()
ACTIVE_RUNS_BY_ID: dict[str, str] = {}
MAX_RUN_DURATION_SECONDS = 4 * 3600


async def start_reaper() -> None:
    """Asyncio task: periodically marks stale IN_PROGRESS runs as ERROR."""
    from ui.grpc_client import (
        get_mpac_stub,
        get_system_grpc_metadata,
    )

    while True:
        await asyncio.sleep(_REAPER_INTERVAL)
        try:
            stub = get_mpac_stub()
            system_metadata = get_system_grpc_metadata()
            if not system_metadata:
                logging.warning(
                    "Reaper: no system metadata (MPAC_JWT_SECRET not set), skipping."
                )
                continue

            loop = asyncio.get_running_loop()
            all_runs = await loop.run_in_executor(
                None,
                lambda: list(
                    stub.ListTestRuns(
                        service_pb2.ListRequest(),
                        metadata=system_metadata,
                    )
                ),
            )
            now_seconds = int(datetime.now(timezone.utc).timestamp())
            for run_pb in all_runs:
                if run_pb.status != service_pb2.ResponseCode.IN_PROGRESS:
                    continue
                age = now_seconds - run_pb.created_at_utc.ToSeconds()
                if age <= MAX_RUN_DURATION_SECONDS:
                    continue
                logging.warning(
                    f"Stale run reaper: marking run {run_pb.id} as ERROR "
                    f"(owner={run_pb.owner}, age={age:.0f}s)"
                )
                run_pb.status = service_pb2.ResponseCode.ERROR
                run_pb.completed_at_utc.GetCurrentTime()
                # Use owner-scoped JWT for UpdateTestRun so ownership check passes.
                from ui.grpc_client import MPAC_JWT_SECRET
                import jwt as pyjwt
                import time

                owner_metadata = []
                if MPAC_JWT_SECRET:
                    payload = {
                        "sub": run_pb.owner,
                        "exp": int(time.time()) + 300,
                        "iat": int(time.time()),
                        "aud": "mpac-system",
                    }
                    owner_token = pyjwt.encode(
                        payload, MPAC_JWT_SECRET, algorithm="HS256"
                    )
                    owner_metadata = [("x-jwt-token", owner_token)]

                await loop.run_in_executor(
                    None,
                    lambda pb=run_pb, m=owner_metadata: stub.UpdateTestRun(
                        pb, metadata=m
                    ),
                )
        except asyncio.CancelledError:
            return
        except Exception:
            logging.error(f"Stale run reaper error:\n{traceback.format_exc()}")
