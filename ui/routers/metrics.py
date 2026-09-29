import json
import traceback

from datetime import date, datetime, time, timedelta, timezone

import duckdb
import pandas as pd

from absl import logging
from fastapi import APIRouter, Depends, Request
from google.protobuf import timestamp_pb2
from google.protobuf.json_format import MessageToDict

from server import service_pb2
from ui.dependencies import (
    UserContext,
    get_current_user,
    get_jwt_token,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub

router = APIRouter()

# Hard ceiling on runs returned for a single date range, so a wide range on a
# chatty test can't blow the page. The template surfaces a notice when hit.
_RUN_RANGE_CAP = 10_000

# Quick-preset durations in days. "custom" means use start_date/end_date params.
_PRESET_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}


def _resolve_range(params) -> tuple[date, date, str]:
    """Return (start_date, end_date, active_preset_key) for the metrics page.

    Default is the trailing 90 days. `range=7d|30d|90d|1y` picks a preset;
    `range=custom` (or any unknown value) honours `start_date` / `end_date`
    in YYYY-MM-DD form when present. Invalid dates fall back to the default.
    """
    today = datetime.now(timezone.utc).date()
    preset = params.get("range") or "90d"

    if preset in _PRESET_DAYS:
        days = _PRESET_DAYS[preset]
        return today - timedelta(days=days), today, preset

    start_raw = params.get("start_date")
    end_raw = params.get("end_date")
    try:
        start = (
            date.fromisoformat(start_raw) if start_raw else today - timedelta(days=90)
        )
        end = date.fromisoformat(end_raw) if end_raw else today
    except ValueError:
        start, end = today - timedelta(days=90), today
    if end < start:
        start, end = end, start
    return start, end, "custom"


def _date_to_timestamp(d: date, end_of_day: bool = False) -> timestamp_pb2.Timestamp:
    ts = timestamp_pb2.Timestamp()
    moment = datetime.combine(
        d, time.max if end_of_day else time.min, tzinfo=timezone.utc
    )
    ts.FromDatetime(moment)
    return ts


@router.get("/metrics")
async def metrics(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    test_id = request.query_params.get("test_id")
    start_date, end_date, active_range = _resolve_range(request.query_params)
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_dicts = await loop.run_in_executor(
            None,
            lambda: [
                MessageToDict(
                    x,
                    always_print_fields_with_no_presence=True,
                    preserving_proto_field_name=True,
                )
                for x in stub.ListTests(
                    service_pb2.ListRequest(limit=100), metadata=metadata
                )
            ],
        )
        test_ids = sorted(set(x.get("id") for x in test_dicts))

        # Request one over the cap so we can tell whether truncation happened
        # without having to issue a separate COUNT query.
        list_runs_request = service_pb2.ListRequest(
            limit=_RUN_RANGE_CAP + 1,
            parent_id=test_id,
            created_after=_date_to_timestamp(start_date),
            created_before=_date_to_timestamp(end_date, end_of_day=True),
        )
        run_dicts = await loop.run_in_executor(
            None,
            lambda: [
                MessageToDict(
                    x,
                    always_print_fields_with_no_presence=True,
                    preserving_proto_field_name=True,
                )
                for x in stub.ListTestRuns(list_runs_request, metadata=metadata)
            ],
        )
        range_cap_hit = len(run_dicts) > _RUN_RANGE_CAP
        if range_cap_hit:
            run_dicts = run_dicts[:_RUN_RANGE_CAP]
        run_df = pd.DataFrame(run_dicts)

        metrics_dicts = []
        for rd in run_dicts:
            metrics_dicts.extend(rd.get("metrics", []))
        metrics_df = pd.DataFrame(metrics_dicts)
        if "modality_scores" in metrics_df.columns:
            metrics_df["modality_scores"] = metrics_df["modality_scores"].apply(
                lambda x: x if isinstance(x, dict) else {}
            )

        if metrics_dicts:
            median_metrics_df = duckdb.sql("""
                SELECT
                    COALESCE(NULLIF(model_id, ''), responder_id) AS responder_id,
                    MEDIAN(f1::FLOAT) AS median_f1,
                    MEDIAN(TRY_CAST(CAST(modality_scores AS JSON)->>'TEXT' AS FLOAT)) AS median_text_f1,
                    MEDIAN(TRY_CAST(CAST(modality_scores AS JSON)->>'IMAGE' AS FLOAT)) AS median_image_f1,
                    MEDIAN(TRY_CAST(CAST(modality_scores AS JSON)->>'AUDIO' AS FLOAT)) AS median_audio_f1,
                    MEDIAN(TRY_CAST(CAST(modality_scores AS JSON)->>'VIDEO' AS FLOAT)) AS median_video_f1,
                    MEDIAN(refusal_error_rate::FLOAT) AS median_refusal_error_rate,
                    MEDIAN(median_task_duration::FLOAT)
                        FILTER (WHERE median_task_duration::FLOAT > 0) AS median_latency_p50,
                    MEDIAN(TRY_CAST(task_duration_p95 AS FLOAT))
                        FILTER (WHERE TRY_CAST(task_duration_p95 AS FLOAT) > 0) AS median_latency_p95,
                    MEDIAN(TRY_CAST(output_tps_p50 AS FLOAT))
                        FILTER (WHERE streamed) AS median_output_tps,
                    MEDIAN(TRY_CAST(ttft_p50 AS FLOAT))
                        FILTER (WHERE TRY_CAST(ttft_p50 AS FLOAT) > 0) AS median_ttft,
                    MEDIAN(TRY_CAST(ttft_p95 AS FLOAT))
                        FILTER (WHERE TRY_CAST(ttft_p95 AS FLOAT) > 0) AS median_ttft_p95,
                    SUM(total::INT) AS total_responses,
                    COUNT(*) AS total_runs,
                    SUM(TRY_CAST(total_cost_usd AS FLOAT)) AS total_cost_usd
                FROM metrics_df
                GROUP BY 1
                ORDER BY 2 DESC
            """).df()
            metrics_json = median_metrics_df.to_json(orient="records")

            daily_stats_df = duckdb.sql("""
                SELECT
                    created_at_utc::DATE AS date,
                    owner,
                    SUM(total_tokens::FLOAT) AS total_tokens,
                    SUM(total_cost_usd::FLOAT) AS total_cost_usd,
                FROM run_df
                GROUP BY 1, 2
                ORDER BY 1
            """).df()
            daily_stats_json = daily_stats_df.to_json(orient="records")

            per_run_metrics = [
                {**m, "test_id": rd.get("test_id", "")}
                for rd in run_dicts
                for m in rd.get("metrics", [])
            ]
            if per_run_metrics:
                per_run_df = pd.DataFrame(per_run_metrics)
                per_test_metrics_df = duckdb.sql("""
                    SELECT
                        COALESCE(NULLIF(model_id, ''), responder_id) AS responder_id,
                        test_id,
                        MEDIAN(f1::FLOAT) AS median_f1,
                        MEDIAN(TRY_CAST(ttft_p50 AS FLOAT))
                            FILTER (WHERE TRY_CAST(ttft_p50 AS FLOAT) > 0) AS median_ttft,
                        MEDIAN(TRY_CAST(output_tps_p50 AS FLOAT))
                            FILTER (WHERE streamed) AS median_output_tps,
                        MEDIAN(refusal_error_rate::FLOAT) AS median_refusal_rate,
                        MEDIAN(median_task_duration::FLOAT)
                            FILTER (WHERE median_task_duration::FLOAT > 0) AS median_latency_p50,
                        SUM(total::INT) AS total_responses,
                        COUNT(*) AS run_count
                    FROM per_run_df
                    GROUP BY 1, 2
                    ORDER BY 1, 3 DESC
                """).df()
                per_test_metrics_json = per_test_metrics_df.to_json(orient="records")
            else:
                per_test_metrics_json = "[]"
        else:
            metrics_json = "[]"
            daily_stats_json = "[]"
            per_test_metrics_json = "[]"

        tests_by_id = {t["id"]: t.get("name", t["id"]) for t in test_dicts}

        try:
            models_meta = await loop.run_in_executor(
                None,
                lambda: {
                    m.id: {
                        "parameters": m.parameters,
                        "parameters_label": m.parameters_label,
                    }
                    for m in stub.ListModels(
                        service_pb2.ListRequest(), metadata=metadata
                    )
                },
            )
        except Exception:
            models_meta = {}

        return templates.TemplateResponse(
            request,
            "metrics.html",
            {
                "metrics_json": metrics_json,
                "daily_stats_json": daily_stats_json,
                "per_test_metrics_json": per_test_metrics_json,
                "tests_by_id_json": json.dumps(tests_by_id),
                "models_meta_json": json.dumps(models_meta),
                "test_dicts": test_dicts,
                "selected_test_id": test_id,
                "active_range": active_range,
                "range_presets": list(_PRESET_DAYS.keys()),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "run_count": len(run_dicts),
                "range_cap": _RUN_RANGE_CAP,
                "range_cap_hit": range_cap_hit,
                "is_admin": user.is_admin,
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )
