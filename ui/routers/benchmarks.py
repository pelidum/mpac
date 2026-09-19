import asyncio
import traceback

import grpc
from absl import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google.protobuf.json_format import MessageToDict

from server import service_pb2
from ui.dependencies import (
    UserContext,
    get_current_user,
    get_jwt_token,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub

router = APIRouter()


def _build_bracket(match_history_list):
    """Reconstruct a static bracket from the server's match_history.

    Each match has: round, id, responses ([{responder, f1, ...}]), winner.
    A match with only one response is a BYE — the model is shown as the winner
    against a null (BYE) slot.
    """
    if not match_history_list:
        return {"rounds": [], "winner": None, "total_models": 0}

    # Group matches by round number
    rounds_by_num: dict = {}
    for m in match_history_list:
        r = int(m.get("round", 0))
        rounds_by_num.setdefault(r, []).append(m)

    if not rounds_by_num:
        return {"rounds": [], "winner": None, "total_models": 0}

    total_rounds = max(rounds_by_num.keys())

    def _round_name(round_num):
        from_end = total_rounds - round_num
        if from_end == 0:
            return "Final"
        if from_end == 1:
            return "Semifinals"
        if from_end == 2:
            return "Quarterfinals"
        if round_num == 1:
            return "First Round"
        return f"Round {round_num}"

    def _conv(resp):
        """Convert BenchmarkMatchResponse dict to the format renderBracket expects."""
        if resp is None:
            return None
        return {
            "responder_id": resp.get("responder", ""),
            "f1": resp.get("f1", 0.0),
            "precision": resp.get("precision", 0.0),
            "recall": resp.get("recall", 0.0),
        }

    rounds = []
    round_names = []
    for r in sorted(rounds_by_num.keys()):
        matchups = []
        for m in sorted(rounds_by_num[r], key=lambda x: int(x.get("id", 0))):
            responses = m.get("responses", [])
            top = _conv(responses[0]) if len(responses) > 0 else None
            bottom = _conv(responses[1]) if len(responses) > 1 else None
            winner_id = m.get("winner", "")
            if winner_id and top and top["responder_id"] == winner_id:
                winner = top
            elif winner_id and bottom and bottom["responder_id"] == winner_id:
                winner = bottom
            else:
                winner = top  # fallback for BYE (single-response match)
            matchups.append({"top": top, "bottom": bottom, "winner": winner})
        rounds.append(matchups)
        round_names.append(_round_name(r))

    # Overall winner from the final round
    final_winner = None
    if rounds:
        for matchup in reversed(rounds[-1]):
            if matchup.get("winner"):
                final_winner = matchup["winner"]
                break

    # Total models = all distinct responders across all matches
    all_responders = {
        resp.get("responder", "")
        for m in match_history_list
        for resp in m.get("responses", [])
        if resp.get("responder")
    }

    return {
        "rounds": rounds,
        "round_names": round_names,
        "winner": final_winner,
        "total_models": len(all_responders),
    }


@router.get("/benchmarks")
async def benchmarks(
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

        benchmark_pbs = await loop.run_in_executor(
            None,
            lambda: list(
                stub.ListBenchmarks(
                    service_pb2.ListRequest(limit=limit), metadata=metadata
                )
            ),
        )
        is_truncated = not show_all and len(benchmark_pbs) >= 500

        test_ids = set(b.test_id for b in benchmark_pbs)
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
            "benchmarks.html",
            {
                "benchmarks": benchmark_pbs,
                "tests_map": tests_map,
                "is_admin": user.is_admin,
                "user_email": user.email,
                "is_truncated": is_truncated,
                "total_shown": len(benchmark_pbs),
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.get("/benchmarks/create")
async def benchmark_create(
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
                [
                    t
                    for t in stub.ListTests(
                        service_pb2.ListRequest(), metadata=metadata
                    )
                    if t.type == 1
                ],
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

        # Compute cost tiers (quintile-based, same logic as models router)
        costs = [
            m.pricing.input_token_cost * 500 + m.pricing.output_token_cost * 25
            for m in model_pbs
        ]
        non_zero = sorted(c for c in costs if c > 0)
        if non_zero:
            quintiles = [non_zero[int(len(non_zero) * i / 5)] for i in range(1, 5)]

            def _tier(c):
                if c == 0:
                    return 0
                elif c <= quintiles[0]:
                    return 1
                elif c <= quintiles[1]:
                    return 2
                elif c <= quintiles[2]:
                    return 3
                elif c <= quintiles[3]:
                    return 4
                return 5
        else:

            def _tier(_):
                return 0

        def _norm_modality(s):
            if not s:
                return "TEXT"
            u = str(s).upper()
            if "IMAGE" in u or "VISION" in u:
                return "VISION"
            if "AUDIO" in u or "SPEECH" in u:
                return "AUDIO"
            return "TEXT"

        model_metadata = {
            m.id: {
                "cost_tier": _tier(costs[i]),
                "modality": _norm_modality(m.capabilities.modality),
            }
            for i, m in enumerate(model_pbs)
        }

        return templates.TemplateResponse(
            request,
            "benchmark_create.html",
            {
                "provider_index": provider_index,
                "backend_index": backend_index,
                "test_pbs": test_pbs,
                "test_id": test_id,
                "model_pricing": model_pricing,
                "model_metadata": model_metadata,
                "is_admin": user.is_admin,
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.post("/benchmarks/submit")
async def benchmark_submit(
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
        benchmark_model_pbs = [m for m in model_pbs if m.id in data.get("models", [])]
        benchmark_request_pb = service_pb2.BenchmarkRequest(
            test_id=data.get("test_id"),
            models=benchmark_model_pbs,
            sample_size=data.get("sample_size"),
            include_reasoning=bool(data.get("reasoning")),
        )

        responses = await loop.run_in_executor(
            None,
            lambda: list(stub.CreateBenchmark(benchmark_request_pb, metadata=metadata)),
        )
        benchmark_id = responses[0].benchmark_id if responses else None

        if not benchmark_id:
            return JSONResponse(
                {"success": False, "error": "Server returned no benchmark ID"},
                status_code=500,
            )

        return JSONResponse(
            {
                "success": True,
                "redirect_url": f"/benchmarks/status/{benchmark_id}",
            }
        )
    except grpc.RpcError as e:
        logging.error(f"gRPC error submitting benchmark: {e}")
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


@router.get("/benchmarks/status/{benchmark_id}")
async def benchmark_status(
    benchmark_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        benchmark_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetBenchmark(
                service_pb2.GetRequest(id=benchmark_id), metadata=metadata
            ),
        )
        if not benchmark_pb.id:
            return templates.TemplateResponse(
                request, "404.html", {"is_admin": user.is_admin}, status_code=404
            )

        if benchmark_pb.status == service_pb2.ResponseCode.SUCCESS:
            return RedirectResponse(url=f"/benchmarks/{benchmark_id}", status_code=303)

        test_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetTest(
                service_pb2.GetRequest(id=benchmark_pb.test_id), metadata=metadata
            ),
        )

        model_count = benchmark_pb.model_count

        created_at_ms = int(benchmark_pb.created_at_utc.ToMilliseconds())

        return templates.TemplateResponse(
            request,
            "benchmark_status.html",
            {
                "benchmark_id": benchmark_id,
                "benchmark_pb": benchmark_pb,
                "test_pb": test_pb,
                "model_count": model_count,
                "created_at_ms": created_at_ms,
                "is_admin": user.is_admin,
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.get("/benchmarks/status/{benchmark_id}/poll")
async def benchmark_status_poll(
    benchmark_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        benchmark_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetBenchmark(
                service_pb2.GetRequest(id=benchmark_id), metadata=metadata
            ),
        )
        benchmark_dict = MessageToDict(
            benchmark_pb,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )

        match_history = benchmark_dict.get("match_history", [])

        for m in match_history:
            for key in ("round", "id"):
                if key in m:
                    m[key] = int(m[key])

        model_count = benchmark_pb.model_count
        import math as _math

        total_rounds = (
            max(1, _math.ceil(_math.log2(model_count))) if model_count >= 2 else 0
        )

        # Current round = highest round with a completed match
        current_round = max((m.get("round", 0) for m in match_history), default=0)

        return JSONResponse(
            {
                "benchmark_id": benchmark_id,
                "status": service_pb2.ResponseCode.Name(benchmark_pb.status),
                "match_history": match_history,
                "model_count": model_count,
                "total_rounds": total_rounds,
                "current_round": current_round,
                "winner": benchmark_dict.get("winner", ""),
            }
        )
    except Exception as e:
        logging.error(f"Error polling benchmark status: {e}")
        return JSONResponse({"error": "An internal error occurred"}, status_code=404)


@router.get("/benchmarks/{benchmark_id}")
async def benchmark_details(
    benchmark_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        benchmark_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetBenchmark(
                service_pb2.GetRequest(id=benchmark_id), metadata=metadata
            ),
        )
        if not benchmark_pb.id:
            return templates.TemplateResponse(
                request, "404.html", {"is_admin": user.is_admin}, status_code=404
            )

        test_pb = None
        try:
            test_pb = await loop.run_in_executor(
                None,
                lambda: stub.GetTest(
                    service_pb2.GetRequest(id=benchmark_pb.test_id),
                    metadata=metadata,
                ),
            )
        except Exception as e:
            logging.warning(f"Failed to fetch test {benchmark_pb.test_id}: {e}")

        benchmark_dict = MessageToDict(
            benchmark_pb,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
        match_history = benchmark_dict.get("match_history", [])
        bracket_data = _build_bracket(match_history)

        # For evaluation tests, enrich each match response with the confusion
        # counts (tp/tn/fp/fn) from its underlying run so the template can
        # render a per-model confusion matrix instead of just F1/P/R pills.
        if test_pb is not None and test_pb.type == 1:
            unique_run_ids = {
                r.get("run_id")
                for m in match_history
                for r in m.get("responses", [])
                if r.get("run_id")
            }

            def _fetch_run_confusion(run_id: str):
                try:
                    run_pb = stub.GetTestRun(
                        service_pb2.GetRequest(id=run_id), metadata=metadata
                    )
                    return run_id, {
                        mtr.responder_id: {
                            "tp": int(mtr.true_positives),
                            "tn": int(mtr.true_negatives),
                            "fp": int(mtr.false_positives),
                            "fn": int(mtr.false_negatives),
                        }
                        for mtr in run_pb.metrics
                    }
                except Exception as ex:
                    logging.warning(f"Failed to fetch confusion for run {run_id}: {ex}")
                    return run_id, {}

            confusion_pairs = await asyncio.gather(
                *[
                    loop.run_in_executor(None, _fetch_run_confusion, rid)
                    for rid in unique_run_ids
                ]
            )
            confusion_by_run = dict(confusion_pairs)

            for m in match_history:
                for resp in m.get("responses", []):
                    counts = confusion_by_run.get(resp.get("run_id"), {}).get(
                        resp.get("responder")
                    )
                    if counts and (
                        counts["tp"] + counts["tn"] + counts["fp"] + counts["fn"] > 0
                    ):
                        resp["confusion"] = counts

        matches_by_round: dict = {}
        for m in match_history:
            r = int(m.get("round", 0))
            matches_by_round.setdefault(r, []).append(m)

        return templates.TemplateResponse(
            request,
            "benchmark_details.html",
            {
                "benchmark_pb": benchmark_pb,
                "test_pb": test_pb,
                "match_history": match_history,
                "matches_by_round": matches_by_round,
                "bracket_data": bracket_data,
                "is_admin": user.is_admin,
                "user_email": user.email,
            },
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.post("/benchmarks/{benchmark_id}/cancel")
async def benchmark_cancel(
    benchmark_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        benchmark_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetBenchmark(
                service_pb2.GetRequest(id=benchmark_id), metadata=metadata
            ),
        )

        if benchmark_pb.status != service_pb2.ResponseCode.IN_PROGRESS:
            return JSONResponse(
                {"success": False, "error": "Benchmark is not in progress"},
                status_code=400,
            )

        benchmark_pb.status = service_pb2.ResponseCode.CANCELLED
        await loop.run_in_executor(
            None,
            lambda: stub.UpdateBenchmark(benchmark_pb, metadata=metadata),
        )
        return JSONResponse({"success": True})
    except grpc.RpcError as e:
        logging.error(f"gRPC error cancelling benchmark: {e}")
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


@router.post("/benchmarks/{benchmark_id}/delete")
async def benchmark_delete(
    benchmark_id: str,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        await loop.run_in_executor(
            None,
            lambda: stub.DeleteBenchmark(
                service_pb2.DeleteRequest(id=benchmark_id), metadata=metadata
            ),
        )
        return JSONResponse({"success": True})
    except grpc.RpcError as e:
        logging.error(f"gRPC error deleting benchmark: {e}")
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
