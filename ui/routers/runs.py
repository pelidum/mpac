import base64
import json
import threading
import time
import traceback
from collections import Counter
from datetime import datetime

import grpc
import pandas as pd

from absl import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict

from server import service_pb2
from ui.auth import (
    create_reconnection_token,
    validate_reconnection_token,
)
from ui.dependencies import (
    UserContext,
    get_current_user,
    get_jwt_token,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub
from ui.state import (
    ACTIVE_RUN_STREAMS,
    ACTIVE_RUN_STREAMS_LOCK,
    ACTIVE_RUNS_BY_ID,
    ACTIVE_RUNS_BY_ID_LOCK,
    MAX_RUN_DURATION_SECONDS,
)

import jwt as pyjwt

router = APIRouter()


@router.get("/runs")
async def runs(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        show_all = request.query_params.get("show_all", "false").lower() == "true"
        limit = 10000 if show_all else 500

        runs_list = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestRuns(
                    service_pb2.ListRequest(limit=limit), metadata=metadata
                )
            ),
        )
        is_truncated = not show_all and len(runs_list) >= 500

        test_ids = set(r.test_id for r in runs_list)
        tests_map = {}
        for tid in test_ids:
            try:
                test_pb = await loop.run_in_executor(
                    None,
                    lambda t=tid: stub.GetTest(
                        service_pb2.GetRequest(id=t), metadata=metadata
                    ),
                )
                tests_map[tid] = test_pb
            except Exception as e:
                logging.warning(f"Failed to fetch test {tid}: {e}")
                tests_map[tid] = None

        return templates.TemplateResponse(
            request,
            "runs.html",
            {
                "runs": runs_list,
                "tests_map": tests_map,
                "is_truncated": is_truncated,
                "total_shown": len(runs_list),
                "is_admin": user.is_admin,
                "user_email": user.email,
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.get("/runs/create")
async def run_config(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        test_id = request.query_params.get("test_id")

        model_pbs = await loop.run_in_executor(
            None,
            lambda: list(stub.ListModels(service_pb2.ListRequest(), metadata=metadata)),
        )
        test_pbs = await loop.run_in_executor(
            None,
            lambda: sorted(
                stub.ListTests(service_pb2.ListRequest(), metadata=metadata),
                key=lambda x: x.name,
            ),
        )

        try:
            backends_list = await loop.run_in_executor(
                None,
                lambda: [
                    x
                    for x in stub.ListBackends(
                        service_pb2.ListRequest(limit=100), metadata=metadata
                    )
                    if x.enabled
                ],
            )
            backends_by_id = {b.id: b.name or b.id for b in backends_list}
        except Exception:
            backends_list = []
            backends_by_id = {}

        provider_index: dict[str, list] = {}
        for m in model_pbs:
            provider_index.setdefault(m.provider, []).append(m)
        provider_index = dict(sorted(provider_index.items()))

        backend_index: dict[str, dict] = {}
        for m in model_pbs:
            bid = m.backend_id or "__unknown__"
            bname = backends_by_id.get(bid, bid)
            if bid not in backend_index:
                backend_index[bid] = {"name": bname, "providers": {}}
            prov = m.provider or "unknown"
            backend_index[bid]["providers"].setdefault(prov, []).append(m.id)

        model_pricing = {
            m.id: {
                "input": m.pricing.input_token_cost,
                "output": m.pricing.output_token_cost,
            }
            for m in model_pbs
        }

        user_daily_usd = user_monthly_usd = user_daily_limit_usd = (
            user_monthly_limit_usd
        ) = 0.0
        try:
            user_pb = await loop.run_in_executor(
                None,
                lambda: stub.GetUser(
                    service_pb2.GetRequest(id=user.email), metadata=metadata
                ),
            )
            user_daily_usd = user_pb.spend.daily_usd
            user_monthly_usd = user_pb.spend.monthly_usd
            if user_pb.rate_limits.daily_spend > 0:
                user_daily_limit_usd = user_pb.rate_limits.daily_spend / 100.0
            if user_pb.rate_limits.monthly_spend > 0:
                user_monthly_limit_usd = user_pb.rate_limits.monthly_spend / 100.0
        except Exception:
            pass

        return templates.TemplateResponse(
            request,
            "run_config.html",
            {
                "provider_index": provider_index,
                "backend_index": backend_index,
                "test_pbs": test_pbs,
                "test_id": test_id,
                "model_pricing": model_pricing,
                "user_daily_usd": user_daily_usd,
                "user_monthly_usd": user_monthly_usd,
                "user_daily_limit_usd": user_daily_limit_usd,
                "user_monthly_limit_usd": user_monthly_limit_usd,
                "is_admin": user.is_admin,
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.post("/runs/submit")
async def run_submit(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    data = await request.json()
    if not data:
        return JSONResponse(
            {"success": False, "error": "Invalid JSON provided"}, status_code=400
        )

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        model_pbs = await loop.run_in_executor(
            None,
            lambda: list(stub.ListModels(service_pb2.ListRequest(), metadata=metadata)),
        )
        run_model_pbs = [m for m in model_pbs if m.id in data.get("models", [])]
        run_request_pb = service_pb2.TestRunRequest(
            test_id=data.get("test_id"),
            models=run_model_pbs,
            sample_size=data.get("sample_size"),
            include_reasoning=data.get("reasoning"),
        )

        run_id = None
        grpc_call = await loop.run_in_executor(
            None,
            lambda: stub.CreateTestRun(run_request_pb, metadata=metadata, timeout=60),
        )
        for initial_reply in grpc_call:
            run_id = initial_reply.run_id
            break

        if not run_id:
            return JSONResponse(
                {"success": False, "error": "Server returned no run ID"},
                status_code=500,
            )

        return JSONResponse({"success": True, "redirect_url": f"/runs/status/{run_id}"})
    except grpc.RpcError as e:
        logging.error(f"gRPC error submitting run: {e}")
        return JSONResponse(
            {"success": False, "error": f"Server error: {e.details()}"}, status_code=500
        )
    except Exception:
        logging.error(traceback.format_exc())
        return JSONResponse(
            {"success": False, "error": "Error processing your request"},
            status_code=500,
        )


@router.get("/runs/status/{run_id}")
async def run_status_by_id(
    run_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        run_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTestRun(
                service_pb2.GetRequest(id=run_id), metadata=metadata
            ),
        )
        if not run_pb.id:
            return templates.TemplateResponse(
                request, "404.html", {"is_admin": user.is_admin}, status_code=404
            )

        if run_pb.status == service_pb2.ResponseCode.SUCCESS:
            return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(
                service_pb2.GetRequest(id=run_pb.test_id), metadata=metadata
            ),
        )
        test_dict = MessageToDict(
            test_pb,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )

        created_at_ms = int(run_pb.created_at_utc.ToMilliseconds())

        return templates.TemplateResponse(
            request,
            "run_status.html",
            {
                "run_id": run_id,
                "models": list(run_pb.models),
                "sample_size": run_pb.sample_size,
                "is_polling_mode": True,
                "test_pb": test_pb,
                "test_dict": test_dict,
                "created_at_ms": created_at_ms,
                "is_admin": user.is_admin,
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.get("/runs/status/{run_id}/poll")
async def run_status_poll(
    run_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        run_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTestRun(
                service_pb2.GetRequest(id=run_id), metadata=metadata
            ),
        )
        run_dict = MessageToDict(
            run_pb,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )

        answer_counts = {}
        if run_pb.status == service_pb2.ResponseCode.IN_PROGRESS:
            try:
                progress_pb = await loop.run_in_executor(
                    None,
                    lambda: stub.GetRunProgress(
                        service_pb2.GetRequest(id=run_id), metadata=metadata
                    ),
                )
                answer_counts = dict(progress_pb.answer_counts)
            except Exception:
                pass

        return JSONResponse(
            {
                "run_id": run_id,
                "status": service_pb2.ResponseCode.Name(run_pb.status),
                "metrics": run_dict.get("metrics", []),
                "answer_counts": answer_counts,
                "sample_size": run_pb.sample_size,
            }
        )
    except Exception as e:
        logging.error(f"Error polling run status: {e}")
        return JSONResponse({"error": "An internal error occurred"}, status_code=404)


@router.get("/runs/{run_id}/download/csv")
async def download_run_csv(
    run_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        run_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTestRun(
                service_pb2.GetRequest(id=run_id), metadata=metadata
            ),
        )
        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(
                service_pb2.GetRequest(id=run_pb.test_id), metadata=metadata
            ),
        )
        answer_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestRunAnswers(
                    service_pb2.ListRequest(
                        parent_id=run_pb.id, limit=run_pb.sample_size
                    ),
                    metadata=metadata,
                )
            ),
        )
        test_item_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestItems(
                    service_pb2.ListRequest(
                        parent_id=run_pb.test_id, limit=test_pb.item_count
                    ),
                    metadata=metadata,
                )
            ),
        )
        items_by_id = {item.id: item for item in test_item_pbs}

        rows = []
        for a in answer_pbs:
            item_pb = items_by_id.get(a.item_id)
            row = {
                "item_id": a.item_id,
                "question": item_pb.question if item_pb else "",
                "context": item_pb.context if item_pb else "",
                "choices": list(item_pb.choices) if item_pb else [],
                "correct_answer": item_pb.answer if item_pb else "",
                "is_relevant": item_pb.is_relevant if item_pb else False,
                "model_id": a.model_id,
                "answer": a.answer,
                "is_correct": a.is_correct,
                "reasoning": a.reasoning,
                "input_tokens": a.input_tokens,
                "output_tokens": a.output_tokens,
                "input_cost": a.input_cost,
                "output_cost": a.output_cost,
                "task_duration": a.task_duration,
            }
            rows.append(row)

        csv_data = pd.DataFrame(rows).to_csv(index=False)
        return StreamingResponse(
            iter([csv_data]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=mpac_run_{run_id}.csv"
            },
        )
    except Exception as e:
        logging.error(f"CSV download failed: {e}")
        return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/runs/{run_id}/download/json")
async def download_run_json(
    run_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        run_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTestRun(
                service_pb2.GetRequest(id=run_id), metadata=metadata
            ),
        )
        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(
                service_pb2.GetRequest(id=run_pb.test_id), metadata=metadata
            ),
        )
        answer_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestRunAnswers(
                    service_pb2.ListRequest(
                        parent_id=run_pb.id, limit=run_pb.sample_size
                    ),
                    metadata=metadata,
                )
            ),
        )
        test_item_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListTestItems(
                    service_pb2.ListRequest(
                        parent_id=run_pb.test_id, limit=test_pb.item_count
                    ),
                    metadata=metadata,
                )
            ),
        )
        items_by_id = {item.id: item for item in test_item_pbs}

        run_dict = MessageToDict(
            run_pb,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
        items = []
        for a in answer_pbs:
            item_pb = items_by_id.get(a.item_id)
            row = MessageToDict(
                a,
                preserving_proto_field_name=True,
                always_print_fields_with_no_presence=True,
            )
            if item_pb:
                row["question"] = item_pb.question
                row["context"] = item_pb.context
                row["choices"] = list(item_pb.choices)
                row["correct_answer"] = item_pb.answer
                row["is_relevant"] = item_pb.is_relevant
            items.append(row)

        run_dict["items"] = items
        json_data = json.dumps(run_dict, indent=2, default=str)
        return StreamingResponse(
            iter([json_data]),
            media_type="application/json",
            headers={
                "Content-Disposition": f"attachment; filename=mpac_run_{run_id}.json"
            },
        )
    except Exception as e:
        logging.error(f"JSON download failed: {e}")
        return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/runs/{run_id}/download/pdf")
async def download_run_pdf(
    run_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    import os
    import tempfile

    import weasyprint

    from ui.server import templates

    try:
        ctx = await _fetch_run_report_data(run_id, jwt_token, num_items=0)
    except Exception as e:
        logging.error(f"PDF download data fetch failed: {e}")
        return JSONResponse({"error": "not found"}, status_code=404)

    tmp_path = None
    try:
        static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
        html_string = templates.get_template("run_report_pdf.html").render(ctx)
        del ctx
        html_doc = weasyprint.HTML(string=html_string, base_url=static_dir)
        del html_string

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp_path = tmp.name
        html_doc.write_pdf(target=tmp)
        tmp.close()
        del html_doc

        def iter_pdf():
            try:
                with open(tmp_path, "rb") as f:
                    while chunk := f.read(65536):
                        yield chunk
            finally:
                os.unlink(tmp_path)

        return StreamingResponse(
            iter_pdf(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=mpac_run_{run_id}.pdf"
            },
        )
    except Exception as e:
        if tmp_path:
            os.unlink(tmp_path)
        logging.error(f"PDF generation failed: {e}")
        return JSONResponse({"error": "PDF generation failed"}, status_code=500)


@router.post("/runs/cancel")
async def run_cancel(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    """Cancel an active SSE run stream by stream_id (sent from client as JSON body)."""
    try:
        data = await request.json() or {}
        stream_id = data.get("stream_id")
        if not stream_id:
            return JSONResponse(
                {"success": False, "error": "No stream_id provided"}, status_code=400
            )

        async with ACTIVE_RUN_STREAMS_LOCK:
            if stream_id not in ACTIVE_RUN_STREAMS:
                return JSONResponse(
                    {"success": False, "error": "No active run for this stream_id"},
                    status_code=404,
                )
            _, grpc_call = ACTIVE_RUN_STREAMS[stream_id]

        grpc_call.cancel()
        return JSONResponse({"success": True, "message": "Run cancelled successfully"})
    except Exception as e:
        logging.error(f"Error cancelling run: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred"}, status_code=500
        )


@router.post("/runs/{run_id}/cancel")
async def cancel_run_by_id(
    run_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        async with ACTIVE_RUNS_BY_ID_LOCK:
            if run_id in ACTIVE_RUNS_BY_ID:
                stream_id = ACTIVE_RUNS_BY_ID[run_id]
                async with ACTIVE_RUN_STREAMS_LOCK:
                    if stream_id in ACTIVE_RUN_STREAMS:
                        _, grpc_call = ACTIVE_RUN_STREAMS[stream_id]
                        grpc_call.cancel()
                        return JSONResponse(
                            {
                                "success": True,
                                "message": "Active job cancelled",
                                "was_active": True,
                            }
                        )

        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        try:
            result_pb = await loop.run_in_executor(
                None,
                lambda: stub.GetTestRun(
                    service_pb2.GetRequest(id=run_id), metadata=metadata
                ),
            )
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return JSONResponse(
                    {"success": False, "error": "Run not found"}, status_code=404
                )
            raise

        if result_pb.status == service_pb2.IN_PROGRESS:
            result_pb.status = service_pb2.CANCELLED
            result_pb.completed_at_utc.GetCurrentTime()
            update_response = await loop.run_in_executor(
                None, lambda: stub.UpdateTestRun(result_pb, metadata=metadata)
            )
            if update_response.code == service_pb2.SUCCESS:
                return JSONResponse(
                    {
                        "success": True,
                        "message": "Stale job marked as cancelled",
                        "was_active": False,
                        "was_stale": True,
                    }
                )
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Failed to update stale job: {update_response.reason}",
                },
                status_code=500,
            )
        elif result_pb.status == service_pb2.SUCCESS:
            return JSONResponse(
                {"success": False, "error": "Job has already completed successfully"},
                status_code=400,
            )
        elif result_pb.status == service_pb2.CANCELLED:
            return JSONResponse(
                {"success": False, "error": "Job is already cancelled"}, status_code=400
            )
        elif result_pb.status == service_pb2.ERROR:
            return JSONResponse(
                {"success": False, "error": "Job has already failed"}, status_code=400
            )
        else:
            return JSONResponse(
                {"success": False, "error": f"Unknown status: {result_pb.status}"},
                status_code=400,
            )

    except Exception as e:
        logging.error(f"Error cancelling run {run_id}: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred"}, status_code=500
        )


@router.post("/runs/{run_id}/delete")
async def run_delete(
    run_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        await loop.run_in_executor(
            None,
            lambda: stub.DeleteTestRun(
                service_pb2.DeleteRequest(id=run_id), metadata=metadata
            ),
        )
        return JSONResponse({"success": True})
    except grpc.RpcError as e:
        logging.error(f"gRPC error deleting run: {e}")
        return JSONResponse(
            {"success": False, "error": f"Server error: {e.details()}"},
            status_code=500,
        )
    except Exception:
        logging.error(traceback.format_exc())
        return JSONResponse(
            {"success": False, "error": "Error processing your request"},
            status_code=500,
        )


def _resize_image_for_pdf(image_bytes: bytes, max_width: int = 400) -> bytes:
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(image_bytes))
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


async def _fetch_run_report_data(
    run_id: str, jwt_token: str | None, num_items: int = 100
) -> dict:
    stub = get_mpac_stub()
    metadata = get_grpc_metadata(jwt_token)
    loop = __import__("asyncio").get_running_loop()

    result_pb = await loop.run_in_executor(
        None,
        lambda: stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=metadata),
    )

    test_pb = None
    try:
        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(
                service_pb2.GetRequest(id=result_pb.test_id), metadata=metadata
            ),
        )
    except Exception as e:
        logging.warning(f"Failed to fetch test {result_pb.test_id}: {e}")

    result_dict = MessageToDict(
        result_pb,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )
    metrics_items = result_dict.get("metrics", [])
    metrics_dict = {x.get("responder_id"): x for x in metrics_items}

    category_winners = {}
    if metrics_items:
        costs = [x.get("total_cost_usd", 0) for x in metrics_items]
        nonzero_costs = [c for c in costs if c > 0]
        category_winners = {
            "precision": max(x.get("precision") for x in metrics_items),
            "recall": max(x.get("recall") for x in metrics_items),
            "f1": max(x.get("f1") for x in metrics_items),
            "throughput": max(
                int(x.get("tokens_per_minute")) for x in metrics_items
            ),  # still tpm; templates divide by 60 for display
            "output_tps": max(x.get("output_tps_p50", 0) or 0 for x in metrics_items),
            "latency_p50": min(
                x.get("median_task_duration", 0) or 0 for x in metrics_items
            ),
            "refusals": min(x.get("refusal_error_rate") for x in metrics_items),
            "cost_min": min(nonzero_costs) if nonzero_costs else None,
        }

    has_modality_scores = any(
        x.get("modality_scores") and len(x.get("modality_scores", {})) > 0
        for x in metrics_items
    )

    ranked_models = sorted(
        metrics_items,
        key=lambda x: x.get("f1", 0) or 0,
        reverse=True,
    )

    rank_order = {m.get("responder_id"): i for i, m in enumerate(ranked_models)}
    models_ranked = sorted(
        result_pb.models,
        key=lambda m: rank_order.get(m.id, len(rank_order)),
    )

    def _parse_ts(ts):
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None

    timeline_spans = [
        (x, _parse_ts(x.get("created_at_utc")), _parse_ts(x.get("completed_at_utc")))
        for x in metrics_items
    ]
    timeline_starts = [s for _, s, _ in timeline_spans if s]
    timeline_ends = [e for _, _, e in timeline_spans if e]
    min_start = min(timeline_starts) if timeline_starts else None
    max_end = max(timeline_ends) if timeline_ends else None
    total_span = (max_end - min_start).total_seconds() if min_start and max_end else 0
    if total_span <= 0:
        total_span = 1
    for x, s, e in timeline_spans:
        if s and e and min_start:
            x["timeline_start_pct"] = max(
                0.0, (s - min_start).total_seconds() / total_span
            )
            x["timeline_duration_pct"] = max(0.0, (e - s).total_seconds() / total_span)
        else:
            x["timeline_start_pct"] = 0.0
            x["timeline_duration_pct"] = 0.0

    effective_limit = num_items if num_items > 0 else (result_pb.sample_size or 100)
    answer_pbs = await loop.run_in_executor(
        None,
        lambda: list(
            stub.ListTestRunAnswers(
                service_pb2.ListRequest(parent_id=result_pb.id, limit=effective_limit),
                metadata=metadata,
            )
        ),
    )
    answer_dicts = [
        MessageToDict(
            a,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
        for a in answer_pbs
    ]

    answers = {}
    for ad in answer_dicts:
        model_id = ad.get("model_id")
        item_id = ad.get("item_id")
        if item_id not in answers:
            answers[item_id] = {"responses": {}, "top_answer": None}
        answers[item_id]["responses"][model_id] = ad

    for item_id, response_dict in answers.items():
        item_answers = [
            x.get("answer") for x in response_dict.get("responses").values() if x
        ]
        counts = Counter(item_answers)
        top = counts.most_common(1)
        answers[item_id]["top_answer"] = top[0][0] if top else None

    for item_id in answers:
        item_pb = await loop.run_in_executor(
            None,
            lambda iid=item_id: stub.GetTestItem(
                service_pb2.GetRequest(id=iid), metadata=metadata
            ),
        )
        answers[item_id]["item_pb"] = item_pb

    attachment_ids = list(
        {
            answers[iid]["item_pb"].attachment_id
            for iid in answers
            if answers[iid]["item_pb"].attachment_id
        }
    )
    has_attachments = bool(attachment_ids)
    attachments_meta = {}
    if attachment_ids:
        try:
            attachment_pbs = await loop.run_in_executor(
                None,
                lambda: list(
                    stub.BatchGetAttachments(
                        service_pb2.BatchGetAttachmentsRequest(
                            ids=attachment_ids, metadata_only=True
                        ),
                        metadata=metadata,
                    )
                ),
            )
            attachments_meta = {a.id: a for a in attachment_pbs}
        except Exception as e:
            logging.warning(f"Failed to fetch attachment metadata: {e}")

    confusion_data = {}
    effective_relevance = {}
    if test_pb and test_pb.type == 1:
        for item_id, item_dict in answers.items():
            item_pb_local = item_dict.get("item_pb")
            if item_pb_local:
                effective_relevance[item_id] = item_pb_local.is_relevant

        stored = {}
        for m in result_pb.metrics:
            total = (
                m.true_positives
                + m.true_negatives
                + m.false_positives
                + m.false_negatives
            )
            if total > 0:
                stored[m.responder_id] = {
                    "tp": m.true_positives,
                    "tn": m.true_negatives,
                    "fp": m.false_positives,
                    "fn": m.false_negatives,
                }
        if stored:
            confusion_data = stored
        else:
            try:
                cm_pb = await loop.run_in_executor(
                    None,
                    lambda: stub.GetTestRunConfusionMatrix(
                        service_pb2.GetRequest(id=result_pb.id),
                        metadata=metadata,
                    ),
                )
                for model_id, counts in cm_pb.per_model.items():
                    confusion_data[model_id] = {
                        "tp": counts.true_positives,
                        "tn": counts.true_negatives,
                        "fp": counts.false_positives,
                        "fn": counts.false_negatives,
                    }
            except Exception as e:
                logging.warning(
                    f"GetTestRunConfusionMatrix failed for {result_pb.id}: {e}"
                )

    attachment_data_uris = {}
    if test_pb and test_pb.type == 1:
        for item_id, item_dict in answers.items():
            item_pb_local = item_dict.get("item_pb")
            if not item_pb_local:
                continue
            is_relevant = item_pb_local.is_relevant
            for resp in item_dict["responses"].values():
                is_correct = resp.get("is_correct", False)
                if is_relevant and is_correct:
                    cat = "tp"
                elif not is_relevant and is_correct:
                    cat = "tn"
                elif not is_relevant and not is_correct:
                    cat = "fp"
                else:
                    cat = "fn"
                resp["category"] = cat

    MODALITY_NAMES = {1: "Audio", 2: "Image", 3: "Video", 4: "Text"}
    modality_metrics = {}
    if test_pb and test_pb.type == 1 and has_attachments:
        item_modality = {}
        for item_id, item_dict in answers.items():
            ipb = item_dict.get("item_pb")
            if ipb and ipb.attachment_id:
                att = attachments_meta.get(ipb.attachment_id)
                item_modality[item_id] = (
                    MODALITY_NAMES.get(att.modality, "Text") if att else "Text"
                )
            else:
                item_modality[item_id] = "Text"

        available_modalities = sorted(set(item_modality.values()))
        if len(available_modalities) > 1:
            for modality in available_modalities:
                mod_items = {iid for iid, m in item_modality.items() if m == modality}
                modality_metrics[modality] = {}
                for model_pb in result_pb.models:
                    mid = model_pb.id
                    tp = tn = fp = fn = total = refusals = 0
                    for iid in mod_items:
                        resp = answers.get(iid, {}).get("responses", {}).get(mid)
                        if not resp:
                            continue
                        total += 1
                        if not resp.get("answer"):
                            refusals += 1
                        cat = resp.get("category", "")
                        if cat == "tp":
                            tp += 1
                        elif cat == "tn":
                            tn += 1
                        elif cat == "fp":
                            fp += 1
                        elif cat == "fn":
                            fn += 1
                    p = tp / (tp + fp) if (tp + fp) else 0
                    r = tp / (tp + fn) if (tp + fn) else 0
                    modality_metrics[modality][mid] = {
                        "precision": p,
                        "recall": r,
                        "f1": (2 * p * r / (p + r)) if (p + r) else 0,
                        "refusal_error_rate": refusals / total if total else 0,
                    }

    attachment_ids_to_embed = {
        answers[iid]["item_pb"].attachment_id
        for iid in answers
        if answers[iid].get("item_pb") and answers[iid]["item_pb"].attachment_id
    }
    if has_attachments:
        for attachment_id in attachment_ids_to_embed:
            try:
                attachment_pb = await loop.run_in_executor(
                    None,
                    lambda aid=attachment_id: stub.GetAttachment(
                        service_pb2.GetRequest(id=aid), metadata=metadata
                    ),
                )
                if attachment_pb.file and attachment_pb.modality == 2:  # IMAGE
                    resized = _resize_image_for_pdf(attachment_pb.file)
                    encoded = base64.b64encode(resized).decode("ascii")
                    attachment_data_uris[attachment_id] = (
                        f"data:image/jpeg;base64,{encoded}"
                    )
            except Exception as e:
                logging.warning(
                    f"Failed to fetch attachment {attachment_id} for report: {e}"
                )

    try:
        run_backends_by_id = await loop.run_in_executor(
            None,
            lambda: {
                b.id: {
                    "name": b.name or b.id,
                    "type": service_pb2.BackendType.Name(b.backend_type).lower(),
                }
                for b in stub.ListBackends(
                    service_pb2.ListRequest(limit=100), metadata=metadata
                )
            },
        )
    except Exception:
        run_backends_by_id = {}

    return {
        "result_pb": result_pb,
        "test_pb": test_pb,
        "metrics_dict": metrics_dict,
        "category_winners": category_winners,
        "has_modality_scores": has_modality_scores,
        "ranked_models": ranked_models,
        "models_ranked": models_ranked,
        "timeline_total_span_seconds": total_span,
        "has_attachments": has_attachments,
        "answers": answers,
        "attachments_meta": attachments_meta,
        "confusion_data": confusion_data,
        "effective_relevance": effective_relevance,
        "backends_by_id": run_backends_by_id,
        "attachment_data_uris": attachment_data_uris,
        "modality_metrics": modality_metrics,
    }


@router.get("/runs/{run_id}")
async def run_details(
    run_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        default_limit = 100
        try:
            limit_param = int(request.query_params.get("num_items", default_limit))
        except ValueError:
            limit_param = default_limit

        ctx = await _fetch_run_report_data(run_id, jwt_token, num_items=limit_param)
        ctx["is_admin"] = user.is_admin
        ctx["user_email"] = user.email
        return templates.TemplateResponse(request, "run_details.html", ctx)
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )
