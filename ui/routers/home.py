import asyncio
import traceback

from datetime import datetime, time, timedelta, timezone

from absl import logging
from fastapi import APIRouter, Depends, Request
from google.protobuf import timestamp_pb2

from server import service_pb2
from ui.dependencies import (
    UserContext,
    get_current_user,
    get_jwt_token,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub

router = APIRouter()

# Window for the home-page Precision/Recall chart. Matches the metrics page
# default so the two surfaces tell a consistent story.
_CHART_WINDOW_DAYS = 90
# Soft ceiling on runs returned for the chart — scatter readability degrades
# past a few hundred points and each run contributes one point per responder.
_CHART_RUN_CAP = 1_000


@router.get("/healthz")
async def healthz():
    return "ok"


@router.get("/")
async def home(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = asyncio.get_running_loop()

        all_tests = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTests(service_pb2.ListRequest(limit=25), metadata=metadata)
            ),
        )
        recent_tests = all_tests[:5]
        tests_map = {t.id: t for t in all_tests}

        chart_window_start = timestamp_pb2.Timestamp()
        chart_window_start.FromDatetime(
            datetime.combine(
                datetime.now(timezone.utc).date() - timedelta(days=_CHART_WINDOW_DAYS),
                time.min,
                tzinfo=timezone.utc,
            )
        )
        runs, benchmarks, chart_runs = await asyncio.gather(
            loop.run_in_executor(
                None,
                lambda: list(
                    stub.ListTestRuns(
                        service_pb2.ListRequest(limit=25), metadata=metadata
                    )
                ),
            ),
            loop.run_in_executor(
                None,
                lambda: list(
                    stub.ListBenchmarks(
                        service_pb2.ListRequest(limit=25), metadata=metadata
                    )
                ),
            ),
            loop.run_in_executor(
                None,
                lambda: list(
                    stub.ListTestRuns(
                        service_pb2.ListRequest(
                            limit=_CHART_RUN_CAP,
                            created_after=chart_window_start,
                        ),
                        metadata=metadata,
                    )
                ),
            ),
        )

        # Collect test IDs we still need
        missing_test_ids = (
            {r.test_id for r in runs if r.test_id and r.test_id not in tests_map}
            | {
                b.test_id
                for b in benchmarks
                if b.test_id and b.test_id not in tests_map
            }
            | {
                r.test_id
                for r in chart_runs
                if r.test_id and r.test_id not in tests_map
            }
        )
        for tid in missing_test_ids:
            try:
                test_pb = await loop.run_in_executor(
                    None,
                    lambda t=tid: stub.GetTest(
                        service_pb2.GetRequest(id=t), metadata=metadata
                    ),
                )
                if test_pb and test_pb.id:
                    tests_map[test_pb.id] = test_pb
            except Exception:
                pass

        combined = []
        for r in runs:
            test_pb = tests_map.get(r.test_id)
            combined.append(
                {
                    "kind": "run",
                    "id": r.id,
                    "test_name": test_pb.name if test_pb and test_pb.name else None,
                    "owner": r.owner,
                    "model_count": len(r.models),
                    "created_at_seconds": r.created_at_utc.ToSeconds(),
                }
            )
        for b in benchmarks:
            test_pb = tests_map.get(b.test_id)
            combined.append(
                {
                    "kind": "benchmark",
                    "id": b.id,
                    "test_name": test_pb.name if test_pb and test_pb.name else None,
                    "owner": b.owner,
                    "model_count": b.model_count,
                    "created_at_seconds": b.created_at_utc.ToSeconds(),
                }
            )

        combined.sort(key=lambda x: x["created_at_seconds"], reverse=True)
        recent_runs = combined[:5]

        metrics_data = []
        for run_pb in chart_runs:
            test_pb = tests_map.get(run_pb.test_id)
            if not run_pb.metrics:
                continue
            if test_pb and test_pb.type != 1:
                continue
            for metrics_pb in run_pb.metrics:
                metrics_data.append(
                    {
                        "precision": metrics_pb.precision,
                        "recall": metrics_pb.recall,
                        "f1": metrics_pb.f1,
                        "refusal_error_rate": metrics_pb.refusal_error_rate,
                        "responder": metrics_pb.responder_id,
                        "test_id": run_pb.test_id,
                        "test_name": test_pb.name if test_pb else run_pb.test_id,
                        "owner": run_pb.owner,
                        "id": run_pb.id,
                        "model_count": len(run_pb.models),
                    }
                )

        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "recent_tests": recent_tests,
                "recent_runs": recent_runs,
                "metrics_data": metrics_data,
                "chart_window_days": _CHART_WINDOW_DAYS,
                "tests_map": tests_map,
                "is_admin": user.is_admin,
                "user": {
                    "email": user.email,
                    "name": user.name,
                    "avatar_url": user.avatar_url,
                },
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )
