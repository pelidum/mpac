import asyncio
import grpc
import random
import statistics
import time
import traceback
import uuid

from absl import logging
from google.protobuf import timestamp_pb2
from sklearn.metrics import precision_score, recall_score, f1_score

from server import service_pb2
from server.objects.backend_pool import get_backend_resources

_CANCEL_EVENTS: dict = {}
# SQL expression for a responder's per-run instance id, falling back to model_id
# for answers written before multi-instance support.
_RESPONDER_KEY_SQL = (
    "COALESCE(NULLIF(proto_jsonb->>'responder_id', ''), proto_jsonb->>'model_id')"
)
_STALE_RUN_THRESHOLD_S = 300
_COST_FLUSH_INTERVAL_S = 30
_HEARTBEAT_INTERVAL_S = 30
_HEARTBEAT_STALE_S = 120
_CONN_ERROR_BREAKER_THRESHOLD = 5


def _responder_key(model_pb) -> str:
    """Per-run responder instance id, falling back to model id for old runs."""
    return model_pb.responder_id or model_pb.id


class RunsMixin:
    async def GetRunProgress(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.RunProgress:
        """Return per-model completed answer counts for a running TestRun."""
        rp = service_pb2.RunProgress()
        if not self.db_pool:
            return rp

        run_bytes = await self._grpc_get(id=request.id, obj_type="test_runs")
        if not run_bytes:
            return rp

        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""SELECT {_RESPONDER_KEY_SQL} AS responder_id,
                              COUNT(*)::int AS cnt
                       FROM test_run_answers
                       WHERE proto_jsonb->>'run_id' = $1
                       GROUP BY 1""",
                    request.id,
                )
                for row in rows:
                    if row["responder_id"]:
                        rp.answer_counts[row["responder_id"]] = row["cnt"]
        except Exception as e:
            logging.error(f"GetRunProgress failed for run {request.id}: {e}")
        return rp

    async def GetTestRunConfusionMatrix(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.TestRunConfusionMatrix:
        """Return per-model TP/TN/FP/FN tallies for the whole run."""
        cm = service_pb2.TestRunConfusionMatrix()
        if not self.db_pool:
            return cm

        run_bytes = await self._grpc_get(id=request.id, obj_type="test_runs")
        if not run_bytes:
            return cm

        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""SELECT {_RESPONDER_KEY_SQL.replace("proto_jsonb", "a.proto_jsonb")} AS responder_id,
                              COUNT(*) FILTER (WHERE (i.proto_jsonb->>'is_relevant')::bool
                                                 AND (a.proto_jsonb->>'is_correct')::bool)::int AS tp,
                              COUNT(*) FILTER (WHERE NOT (i.proto_jsonb->>'is_relevant')::bool
                                                 AND (a.proto_jsonb->>'is_correct')::bool)::int AS tn,
                              COUNT(*) FILTER (WHERE NOT (i.proto_jsonb->>'is_relevant')::bool
                                                 AND NOT (a.proto_jsonb->>'is_correct')::bool)::int AS fp,
                              COUNT(*) FILTER (WHERE (i.proto_jsonb->>'is_relevant')::bool
                                                 AND NOT (a.proto_jsonb->>'is_correct')::bool)::int AS fn
                       FROM test_run_answers a
                       JOIN test_items i ON i.id = a.proto_jsonb->>'item_id'
                       WHERE a.proto_jsonb->>'run_id' = $1
                       GROUP BY 1""",
                    request.id,
                )
                for row in rows:
                    if not row["responder_id"]:
                        continue
                    counts = cm.per_model[row["responder_id"]]
                    counts.true_positives = row["tp"]
                    counts.true_negatives = row["tn"]
                    counts.false_positives = row["fp"]
                    counts.false_negatives = row["fn"]
        except Exception as e:
            logging.error(f"GetTestRunConfusionMatrix failed for run {request.id}: {e}")
        return cm

    async def GetTestRun(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.TestRun:
        try:
            run_pb = service_pb2.TestRun()
            run_bytes = await self._grpc_get(
                id=request.id,
                obj_type="test_runs",
            )
            if run_bytes:
                run_pb.ParseFromString(run_bytes)
            else:
                return run_pb

            if run_pb.test_id:
                parent_test = await self._grpc_get(id=run_pb.test_id, obj_type="tests")
                if not parent_test:
                    await context.abort(
                        grpc.StatusCode.NOT_FOUND,
                        "Test run not found.",
                    )

            return run_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )

    @staticmethod
    def _reconstruct_sample(
        all_item_pbs: list, completed_pairs: set, sample_size: int
    ) -> list:
        """Rebuild a resumed run's item sample.

        The original random sample isn't persisted, so keep every item that
        already has an answer (they were definitely in the sample) and fill
        the remainder randomly up to sample_size. Without this cap a resumed
        sample_size=10 run on a large test would burn quota on every item in
        the test — and skew metrics denominators.
        """
        effective_size = sample_size if sample_size > 0 else 10
        answered_item_ids = {item_id for _, item_id in completed_pairs}
        answered_items = [i for i in all_item_pbs if i.id in answered_item_ids]
        unanswered_items = [i for i in all_item_pbs if i.id not in answered_item_ids]
        random.shuffle(unanswered_items)
        fill_count = max(0, effective_size - len(answered_items))
        return answered_items + unanswered_items[:fill_count]

    async def _run_watchdog(self, run_id: str, cancel_event: asyncio.Event) -> None:
        """Heartbeat + cancellation poll for a live run.

        Every _HEARTBEAT_INTERVAL_S: bump heartbeat_at (proves this instance
        still owns the run, so resume_stale_runs won't claim it) and read the
        DB status in the same round trip. Honoring the DB status here is what
        makes cancellation work across instances — UpdateTestRun(CANCELLED)
        may land on an instance that doesn't hold this run's cancel_event.
        """
        if not self.db_pool:
            return
        while not cancel_event.is_set():
            try:
                async with self.db_pool.acquire() as conn:
                    status = await conn.fetchval(
                        """UPDATE test_runs SET heartbeat_at = NOW()
                           WHERE id = $1
                           RETURNING proto_jsonb->>'status'""",
                        run_id,
                    )
                if status is None:
                    # Run row deleted out from under us — stop working on it.
                    logging.warning(f"Run {run_id} deleted mid-flight, stopping")
                    cancel_event.set()
                    return
                if status == "CANCELLED":
                    cancel_event.set()
                    return
            except Exception as e:
                logging.warning(f"Watchdog heartbeat failed for run {run_id}: {e}")
            try:
                await asyncio.wait_for(
                    cancel_event.wait(), timeout=_HEARTBEAT_INTERVAL_S
                )
            except asyncio.TimeoutError:
                pass

    async def _do_run_inference(
        self,
        run_pb: service_pb2.TestRun,
        test_pb,
        sample_item_pbs: list,
        request: service_pb2.TestRunRequest,
        cancel_event: asyncio.Event,
        completed_pairs: set | None = None,
    ) -> None:
        """Background task: run inference for all models and write results to DB.

        Decoupled from any gRPC context; uses cancel_event for cooperative
        cancellation rather than context.cancelled().
        """
        _backend_cache: dict[str, service_pb2.Backend] = {}

        async def _get_backend(backend_id: str) -> service_pb2.Backend:
            if backend_id not in _backend_cache:
                bp = service_pb2.Backend()
                if backend_id:
                    raw = await self._grpc_get(id=backend_id, obj_type="backends")
                    if raw:
                        bp.ParseFromString(raw)
                    else:
                        logging.warning(f"Backend {backend_id} not found in DB")
                _backend_cache[backend_id] = bp
            return _backend_cache[backend_id]

        async def throttled_answer_task(sample_pb, model_pb, backend_pb):
            nonlocal _run_cost
            resources = await get_backend_resources(backend_pb)
            try:
                async with resources.acquire():
                    if cancel_event.is_set():
                        raise asyncio.CancelledError("Run cancelled")
                    answer_pb = await self.answer_test_item(
                        item_pb=sample_pb,
                        run_id=run_pb.id,
                        model_pb=model_pb,
                        backend=backend_pb,
                        context=None,
                        include_reasoning=request.include_reasoning,
                    )
                total_tokens = answer_pb.input_tokens + answer_pb.output_tokens
                if total_tokens > 0:
                    within_limit = await self.record_and_check_tpm(
                        run_pb.owner, total_tokens
                    )
                    if not within_limit:
                        seconds_remaining = 60 - (int(time.time()) % 60)
                        await asyncio.sleep(seconds_remaining + 1)

                answer_cost = answer_pb.input_cost + answer_pb.output_cost
                _run_cost += answer_cost
                try:
                    limits = await self._get_user_rate_limits(run_pb.owner)
                    if limits.daily_spend > 0:
                        if (
                            int((baseline_daily + _run_cost) * 100)
                            >= limits.daily_spend
                        ):
                            cancel_event.set()
                    if limits.monthly_spend > 0:
                        if (
                            int((baseline_monthly + _run_cost) * 100)
                            >= limits.monthly_spend
                        ):
                            cancel_event.set()
                except Exception:
                    logging.warning("Mid-run spend check failed, skipping")

                return answer_pb
            except asyncio.TimeoutError:
                logging.warning(
                    f"Backend {backend_pb.id} at capacity for item {sample_pb.id}"
                )
                failed_pb = service_pb2.TestRunAnswer()
                failed_pb.id = uuid.uuid4().hex
                failed_pb.run_id = run_pb.id
                failed_pb.item_id = sample_pb.id
                failed_pb.model_id = model_pb.id
                failed_pb.responder_id = _responder_key(model_pb)
                failed_pb.reasoning = (
                    "Backend at capacity - request waited too long for a slot"
                )
                failed_pb.raw_response = "[CAPACITY] Backend at capacity - request waited too long for a slot"
                return failed_pb

        _run_cost = 0.0
        _last_cost_flush = time.monotonic()
        watchdog = asyncio.create_task(
            self._run_watchdog(run_pb.id, cancel_event),
            name=f"watchdog-{run_pb.id}",
        )
        try:
            total_cost = 0
            total_tokens = 0
            try:
                baseline_daily, baseline_monthly = await self._get_current_spend(
                    run_pb.owner
                )
            except Exception:
                logging.warning("Failed to read spend baseline, using zero")
                baseline_daily, baseline_monthly = 0.0, 0.0
            actual_sample_size = len(sample_item_pbs)

            for model_pb in request.models:
                if cancel_event.is_set():
                    raise asyncio.CancelledError("Run cancelled")

                responder_key = _responder_key(model_pb)
                backend_pb = await _get_backend(
                    model_pb.backend_id or run_pb.backend_id
                )

                items_to_run = [
                    s
                    for s in sample_item_pbs
                    if not completed_pairs
                    or (responder_key, s.id) not in completed_pairs
                ]
                model_tasks = [
                    asyncio.ensure_future(
                        throttled_answer_task(sample_pb, model_pb, backend_pb)
                    )
                    for sample_pb in items_to_run
                ]
                model_answers = []
                if completed_pairs:
                    async with self.db_pool.acquire() as conn:
                        prior_rows = await conn.fetch(
                            f"""SELECT proto_bytes FROM test_run_answers
                               WHERE proto_jsonb->>'run_id' = $1
                                 AND {_RESPONDER_KEY_SQL} = $2""",
                            run_pb.id,
                            responder_key,
                        )
                    for pr in prior_rows:
                        a = service_pb2.TestRunAnswer()
                        a.ParseFromString(pr["proto_bytes"])
                        model_answers.append(a)
                model_start_time = time.perf_counter()
                model_start_pb = timestamp_pb2.Timestamp()
                model_start_pb.GetCurrentTime()
                last_answer_time = None
                startup_latency = None
                answered_item_ids = {a.item_id for a in model_answers}
                consecutive_conn_errors = 0
                breaker_tripped = False

                for answer in asyncio.as_completed(model_tasks):
                    if cancel_event.is_set():
                        for task in model_tasks:
                            if not task.done():
                                task.cancel()
                        raise asyncio.CancelledError("Run cancelled")

                    answer_pb = await answer
                    await self._grpc_create(
                        id=answer_pb.id,
                        proto_obj=answer_pb,
                        obj_type="test_run_answers",
                        overwrite=False,
                    )

                    answer_complete_time = time.perf_counter()
                    if not last_answer_time:
                        startup_latency = answer_complete_time - model_start_time
                    last_answer_time = answer_complete_time
                    model_answers.append(answer_pb)
                    answered_item_ids.add(answer_pb.item_id)

                    if answer_pb.raw_response.startswith("[CONNECTION_ERROR]"):
                        consecutive_conn_errors += 1
                        if consecutive_conn_errors >= _CONN_ERROR_BREAKER_THRESHOLD:
                            breaker_tripped = True
                            break
                    else:
                        consecutive_conn_errors = 0

                    if time.monotonic() - _last_cost_flush >= _COST_FLUSH_INTERVAL_S:
                        run_pb.total_cost_usd = _run_cost
                        await self._grpc_update(
                            id=run_pb.id, proto_obj=run_pb, obj_type="test_runs"
                        )
                        try:
                            cur_daily, cur_monthly = await self._get_current_spend(
                                run_pb.owner
                            )
                            limits = await self._get_user_rate_limits(run_pb.owner)
                            if limits.daily_spend > 0:
                                if int(cur_daily * 100) >= limits.daily_spend:
                                    cancel_event.set()
                            if limits.monthly_spend > 0:
                                if int(cur_monthly * 100) >= limits.monthly_spend:
                                    cancel_event.set()
                        except Exception:
                            logging.error(traceback.format_exc())
                        _last_cost_flush = time.monotonic()

                if breaker_tripped:
                    logging.error(
                        f"Run {run_pb.id}: circuit breaker open for model "
                        f"{model_pb.id} after {consecutive_conn_errors} consecutive "
                        f"connection errors; skipping remaining items"
                    )
                    for task in model_tasks:
                        if not task.done():
                            task.cancel()
                    reaped = await asyncio.gather(*model_tasks, return_exceptions=True)
                    for res in reaped:
                        if (
                            isinstance(res, service_pb2.TestRunAnswer)
                            and res.item_id not in answered_item_ids
                        ):
                            await self._grpc_create(
                                id=res.id,
                                proto_obj=res,
                                obj_type="test_run_answers",
                                overwrite=False,
                            )
                            model_answers.append(res)
                            answered_item_ids.add(res.item_id)
                    for sample_pb in items_to_run:
                        if sample_pb.id in answered_item_ids:
                            continue
                        skipped_pb = service_pb2.TestRunAnswer()
                        skipped_pb.id = uuid.uuid4().hex
                        skipped_pb.run_id = run_pb.id
                        skipped_pb.item_id = sample_pb.id
                        skipped_pb.model_id = model_pb.id
                        skipped_pb.responder_id = responder_key
                        skipped_pb.reasoning = (
                            "Skipped - backend unreachable (circuit breaker open "
                            "after repeated connection errors)"
                        )
                        skipped_pb.raw_response = (
                            "[SKIPPED] Backend unreachable - circuit breaker open"
                        )
                        await self._grpc_create(
                            id=skipped_pb.id,
                            proto_obj=skipped_pb,
                            obj_type="test_run_answers",
                            overwrite=False,
                        )
                        model_answers.append(skipped_pb)
                        answered_item_ids.add(sample_pb.id)

                # Model complete — compute metrics and persist
                model_end_pb = timestamp_pb2.Timestamp()
                model_end_pb.GetCurrentTime()
                num_refusals = len([x for x in model_answers if not x.answer])
                if test_pb.type == 1:
                    num_correct = len([x for x in model_answers if x.is_correct])
                    num_positives = len([x for x in sample_item_pbs if x.is_relevant])
                    simple_percentage_score = (
                        num_correct / actual_sample_size if actual_sample_size else 0.0
                    )

                attachment_modalities = list(
                    set([x.attachment_type for x in model_answers])
                )
                attachment_metrics = {}
                if test_pb.type == 1:
                    for attachment_modality in attachment_modalities:
                        modality_name = service_pb2.FileModality.Name(
                            attachment_modality
                        )
                        modality_correct = len(
                            [
                                x
                                for x in model_answers
                                if x.attachment_type == attachment_modality
                                and x.is_correct
                            ]
                        )
                        modality_total = len(
                            [
                                x
                                for x in model_answers
                                if x.attachment_type == attachment_modality
                            ]
                        )
                        attachment_metrics[modality_name] = (
                            modality_correct / modality_total
                        )

                task_durations = [x.task_duration for x in model_answers]
                median_task_duration = (
                    statistics.median(task_durations) if task_durations else 0.0
                )
                if len(task_durations) >= 2:
                    p95_quants = statistics.quantiles(task_durations, n=20)
                    task_duration_p95 = p95_quants[18]
                else:
                    task_duration_p95 = task_durations[0] if task_durations else 0.0
                if len(task_durations) >= 2:
                    p99_quants = statistics.quantiles(task_durations, n=100)
                    task_duration_p99 = p99_quants[98]
                else:
                    task_duration_p99 = task_durations[0] if task_durations else 0.0

                per_answer_tps = [
                    x.output_tokens / x.task_duration
                    for x in model_answers
                    if x.output_tokens > 0 and x.task_duration > 0
                ]
                if per_answer_tps:
                    output_tps_p50 = statistics.median(per_answer_tps)
                    if len(per_answer_tps) >= 2:
                        tps_quants = statistics.quantiles(per_answer_tps, n=20)
                        output_tps_p95 = tps_quants[18]
                    else:
                        output_tps_p95 = per_answer_tps[0]
                else:
                    output_tps_p50 = 0.0
                    output_tps_p95 = 0.0

                model_wall_time = (
                    (last_answer_time - model_start_time) if last_answer_time else 0.0
                )
                total_output_tokens_for_model = sum(
                    x.output_tokens for x in model_answers if x.output_tokens > 0
                )
                output_tpm = (
                    int(total_output_tokens_for_model / model_wall_time * 60)
                    if model_wall_time > 0
                    else 0
                )
                model_total_tokens = 0
                model_total_cost = 0.0
                for model_answer in model_answers:
                    model_total_tokens += (
                        model_answer.input_tokens + model_answer.output_tokens
                    )
                    model_total_cost += (
                        model_answer.input_cost + model_answer.output_cost
                    )
                    total_tokens += (
                        model_answer.input_tokens + model_answer.output_tokens
                    )
                    total_cost += model_answer.input_cost + model_answer.output_cost

                metrics_pb = service_pb2.TestRun.TestRunMetrics()
                metrics_pb.responder_id = responder_key
                metrics_pb.model_id = model_pb.id

                if test_pb.type == 1:
                    metrics_pb.simple_grade = self.calculate_grade(
                        simple_percentage_score
                    )
                    metrics_pb.num_positives = num_positives
                    metrics_pb.num_correct = num_correct

                metrics_pb.num_refusals_errors = num_refusals
                metrics_pb.total = actual_sample_size
                metrics_pb.refusal_error_rate = (
                    num_refusals / actual_sample_size if actual_sample_size else 0.0
                )
                metrics_pb.tokens_per_minute = output_tpm
                metrics_pb.median_task_duration = median_task_duration
                metrics_pb.task_duration_p95 = task_duration_p95
                metrics_pb.task_duration_p99 = task_duration_p99
                metrics_pb.output_tps_p50 = output_tps_p50
                metrics_pb.output_tps_p95 = output_tps_p95
                metrics_pb.startup_latency = startup_latency or 0.0
                metrics_pb.modality_scores.update(attachment_metrics)
                metrics_pb.total_cost_usd = model_total_cost
                metrics_pb.total_tokens = model_total_tokens
                metrics_pb.created_at_utc.CopyFrom(model_start_pb)
                metrics_pb.completed_at_utc.CopyFrom(model_end_pb)

                if test_pb.type == 1:
                    answer_map = {x.item_id: {"response": x} for x in model_answers}
                    for sample_item_pb in sample_item_pbs:
                        answer_map[sample_item_pb.id]["item"] = sample_item_pb

                    ground_truth = []
                    responder_answers = []
                    for item_id, item_dict in answer_map.items():
                        ground_truth.append(item_dict["item"].answer)
                        responder_answers.append(item_dict["response"].answer)

                    metrics_pb.precision = precision_score(
                        y_true=ground_truth,
                        y_pred=responder_answers,
                        average="weighted",
                        zero_division=0,
                    )
                    metrics_pb.recall = recall_score(
                        y_true=ground_truth,
                        y_pred=responder_answers,
                        average="weighted",
                        zero_division=0,
                    )
                    metrics_pb.f1 = f1_score(
                        y_true=ground_truth,
                        y_pred=responder_answers,
                        average="weighted",
                        zero_division=0,
                    )

                    tp = tn = fp = fn = 0
                    for item_dict in answer_map.values():
                        is_relevant = item_dict["item"].is_relevant
                        is_correct = item_dict["response"].is_correct
                        if is_relevant and is_correct:
                            tp += 1
                        elif not is_relevant and is_correct:
                            tn += 1
                        elif not is_relevant and not is_correct:
                            fp += 1
                        else:
                            fn += 1
                    metrics_pb.true_positives = tp
                    metrics_pb.true_negatives = tn
                    metrics_pb.false_positives = fp
                    metrics_pb.false_negatives = fn

                run_pb.metrics.append(metrics_pb)
                if cancel_event.is_set():
                    raise asyncio.CancelledError("Run cancelled")
                await self._grpc_update(
                    id=run_pb.id, proto_obj=run_pb, obj_type="test_runs"
                )

            # All models complete
            run_pb.completed_at_utc.GetCurrentTime()
            run_pb.total_cost_usd = total_cost
            run_pb.total_tokens = total_tokens

            if backend_pb.backend_type == service_pb2.BackendType.DEBUG_RANDOM:
                run_pb.total_cost_usd = 0.001

            _LOCAL_BACKENDS = {
                service_pb2.BackendType.OLLAMA,
                service_pb2.BackendType.LLAMA_CPP,
                service_pb2.BackendType.VLLM,
            }
            if (
                run_pb.total_cost_usd == 0
                and backend_pb.backend_type in _LOCAL_BACKENDS
            ):
                duration_seconds = (
                    run_pb.completed_at_utc.ToSeconds()
                    - run_pb.created_at_utc.ToSeconds()
                )
                local_inference_cost = self.estimate_local_inference_cost(
                    duration_seconds=duration_seconds,
                )
                run_pb.total_cost_usd = local_inference_cost

            run_pb.status = service_pb2.ResponseCode.SUCCESS
            await self._grpc_update(
                id=run_pb.id,
                proto_obj=run_pb,
                obj_type="test_runs",
            )

        except asyncio.CancelledError:
            if cancel_event.is_set():
                # Deliberate cancellation (user request or spend limit).
                run_pb.completed_at_utc.GetCurrentTime()
                run_pb.total_cost_usd = _run_cost
                run_pb.status = service_pb2.ResponseCode.CANCELLED
                await self._grpc_update(
                    id=run_pb.id,
                    proto_obj=run_pb,
                    obj_type="test_runs",
                )
            else:
                logging.info(
                    f"Run {run_pb.id} interrupted by shutdown; "
                    f"leaving IN_PROGRESS for resume"
                )
                raise

        except Exception:
            logging.error(f"Run {run_pb.id} failed with unhandled exception:")
            logging.error(traceback.format_exc())
            run_pb.completed_at_utc.GetCurrentTime()
            run_pb.total_cost_usd = _run_cost
            run_pb.status = service_pb2.ResponseCode.ERROR
            await self._salvage_partial_metrics(run_pb, test_pb, sample_item_pbs)
            await self._grpc_update(
                id=run_pb.id,
                proto_obj=run_pb,
                obj_type="test_runs",
            )

        finally:
            watchdog.cancel()
            _CANCEL_EVENTS.pop(run_pb.id, None)

    async def _salvage_partial_metrics(
        self,
        run_pb: service_pb2.TestRun,
        test_pb,
        sample_item_pbs: list,
    ) -> None:
        """Compute metrics for models with answers when a run errors.

        Models that already have metrics in run_pb are skipped. For each
        remaining model, answers are loaded from the DB and P/R/F1 is
        computed over whatever items were completed.
        """
        if not self.db_pool or test_pb.type != 1 or not sample_item_pbs:
            return

        already_computed = {m.responder_id for m in run_pb.metrics}
        responder_to_model = {_responder_key(m): m.id for m in run_pb.models}
        all_responder_ids = set(responder_to_model)
        missing = all_responder_ids - already_computed
        if not missing:
            return

        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT proto_bytes FROM test_run_answers
                       WHERE proto_jsonb->>'run_id' = $1""",
                    run_pb.id,
                )
        except Exception as e:
            logging.warning(f"Failed to load answers for partial metrics: {e}")
            return

        answers_by_model: dict[str, list] = {}
        for row in rows:
            answer_pb = service_pb2.TestRunAnswer()
            answer_pb.ParseFromString(row["proto_bytes"])
            responder_key = answer_pb.responder_id or answer_pb.model_id
            if responder_key in missing:
                answers_by_model.setdefault(responder_key, []).append(answer_pb)

        sample_items_by_id = {s.id: s for s in sample_item_pbs}

        for responder_key, model_answers in answers_by_model.items():
            if not model_answers:
                continue
            try:
                answer_map = {}
                for a in model_answers:
                    item = sample_items_by_id.get(a.item_id)
                    if item:
                        answer_map[a.item_id] = {"response": a, "item": item}

                if not answer_map:
                    continue

                ground_truth = []
                responder_answers = []
                for item_dict in answer_map.values():
                    ground_truth.append(item_dict["item"].answer)
                    responder_answers.append(item_dict["response"].answer)

                metrics_pb = service_pb2.TestRun.TestRunMetrics()
                metrics_pb.responder_id = responder_key
                metrics_pb.model_id = responder_to_model.get(
                    responder_key, responder_key
                )
                metrics_pb.total = len(answer_map)
                metrics_pb.num_correct = len(
                    [d for d in answer_map.values() if d["response"].is_correct]
                )
                metrics_pb.num_refusals_errors = len(
                    [a for a in model_answers if not a.answer]
                )
                metrics_pb.precision = precision_score(
                    y_true=ground_truth,
                    y_pred=responder_answers,
                    average="weighted",
                    zero_division=0,
                )
                metrics_pb.recall = recall_score(
                    y_true=ground_truth,
                    y_pred=responder_answers,
                    average="weighted",
                    zero_division=0,
                )
                metrics_pb.f1 = f1_score(
                    y_true=ground_truth,
                    y_pred=responder_answers,
                    average="weighted",
                    zero_division=0,
                )
                run_pb.metrics.append(metrics_pb)
                logging.info(
                    f"Run {run_pb.id}: salvaged partial metrics for {responder_key} "
                    f"({len(answer_map)} of {len(sample_item_pbs)} items)"
                )
            except Exception as e:
                logging.warning(
                    f"Failed to compute partial metrics for {responder_key}: {e}"
                )

    async def CreateTestRun(
        self,
        request: service_pb2.TestRunRequest,
        context: grpc.aio.ServicerContext,
    ):
        request_owner = await self.get_request_owner(request)
        if not request_owner:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED, "No credentials provided."
            )

        await self.check_spend_limits(user_id=request_owner, context=context)

        # Pre-flight: reject runs whose projected cost exceeds remaining budget.
        _preflight_backend = service_pb2.Backend()
        if request.backend_id:
            _raw_backend = await self._grpc_get(
                id=request.backend_id, obj_type="backends"
            )
            if _raw_backend:
                _preflight_backend.ParseFromString(_raw_backend)
        projected = await self._projected_run_cost(request, _preflight_backend)
        await self.check_projected_cost(request_owner, projected, context)

        run_pb = service_pb2.TestRun()
        run_pb.id = uuid.uuid4().hex
        run_pb.test_id = request.test_id
        run_pb.owner = request_owner
        run_pb.status = service_pb2.ResponseCode.IN_PROGRESS
        run_pb.models.extend(request.models)
        run_pb.include_reasoning = request.include_reasoning
        run_pb.sample_size = request.sample_size
        run_pb.backend_id = request.backend_id
        run_pb.created_at_utc.GetCurrentTime()

        await self._grpc_create(
            id=run_pb.id,
            proto_obj=run_pb,
            obj_type="test_runs",
            overwrite=False,
        )

        test_pb = await self.GetTest(
            request=service_pb2.GetRequest(id=request.test_id),
            context=context,
        )

        sample_item_pbs = []
        query_string = """
            SELECT
                proto_bytes,
                RANDOM() as rand
            FROM test_items
            WHERE proto_jsonb ->> 'test_id' = $1
            ORDER BY rand
            LIMIT $2
            """

        sample_size = request.sample_size if request.sample_size > 0 else 10
        query_values = (request.test_id, sample_size)
        try:
            async with self.db_pool.acquire() as conn:
                proto_results = await conn.fetch(query_string, *query_values)
                for proto_result in proto_results:
                    sample_item_bytes = proto_result["proto_bytes"]
                    sample_item_pb = service_pb2.TestItem()
                    sample_item_pb.ParseFromString(sample_item_bytes)
                    sample_item_pbs.append(sample_item_pb)
        except Exception as e:
            logging.error(traceback.format_exc())
            logging.error(f"Unable to fetch object: {e}")
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )

        cancel_event = asyncio.Event()
        _CANCEL_EVENTS[run_pb.id] = cancel_event

        asyncio.create_task(
            self._do_run_inference(
                run_pb, test_pb, sample_item_pbs, request, cancel_event
            ),
            name=f"run-{run_pb.id}",
        )

        run_reply_pb = service_pb2.TestRunReply()
        run_reply_pb.run_id = run_pb.id
        run_reply_pb.test_id = test_pb.id
        run_reply_pb.code = service_pb2.ResponseCode.IN_PROGRESS
        yield run_reply_pb

    async def resume_stale_runs(self) -> None:
        """Detect and resume runs orphaned by instance recycling.

        Called at server startup and periodically thereafter. A run is
        claimable when its heartbeat is stale (the owning instance's watchdog
        stopped bumping it — instance died or was scaled down). The claim is
        a single conditional UPDATE, so concurrent sweeps on different
        instances can never both resume the same run.
        """
        if not self.db_pool:
            return

        try:
            rows = await self.db_pool.fetch(
                """SELECT id, proto_bytes FROM test_runs
                   WHERE proto_jsonb->>'status' = 'IN_PROGRESS'
                     AND (heartbeat_at IS NULL
                          OR heartbeat_at < NOW() - make_interval(secs => $1))
                     AND created_at < NOW() - make_interval(secs => $2)""",
                float(_HEARTBEAT_STALE_S),
                float(_STALE_RUN_THRESHOLD_S),
            )
        except Exception as e:
            logging.error(f"Failed to query for stale runs: {e}")
            return

        for row in rows:
            run_pb = service_pb2.TestRun()
            run_pb.ParseFromString(row["proto_bytes"])
            try:
                async with self.db_pool.acquire() as conn:
                    claimed = await conn.fetchval(
                        """UPDATE test_runs SET heartbeat_at = NOW()
                           WHERE id = $1
                             AND proto_jsonb->>'status' = 'IN_PROGRESS'
                             AND (heartbeat_at IS NULL
                                  OR heartbeat_at < NOW() - make_interval(secs => $2))
                           RETURNING id""",
                        run_pb.id,
                        float(_HEARTBEAT_STALE_S),
                    )
                if not claimed:
                    logging.info(f"Run {run_pb.id} claimed by another instance")
                    continue
            except Exception as e:
                logging.error(f"Failed to claim stale run {run_pb.id}: {e}")
                continue

            completed_pairs = set()
            try:
                async with self.db_pool.acquire() as conn:
                    answer_rows = await conn.fetch(
                        f"""SELECT proto_jsonb->>'item_id' AS item_id,
                                  {_RESPONDER_KEY_SQL} AS responder_id
                           FROM test_run_answers
                           WHERE proto_jsonb->>'run_id' = $1""",
                        run_pb.id,
                    )
                for r in answer_rows:
                    completed_pairs.add((r["responder_id"], r["item_id"]))
            except Exception as e:
                logging.error(f"Failed to load completed answers for {run_pb.id}: {e}")
                continue

            test_pb = service_pb2.Test()
            try:
                raw = await self._grpc_get(id=run_pb.test_id, obj_type="tests")
                if raw:
                    test_pb.ParseFromString(raw)
            except Exception as e:
                logging.error(f"Failed to load test for run {run_pb.id}: {e}")
                continue

            all_item_pbs = []
            try:
                async with self.db_pool.acquire() as conn:
                    item_rows = await conn.fetch(
                        """SELECT proto_bytes FROM test_items
                           WHERE proto_jsonb->>'test_id' = $1""",
                        run_pb.test_id,
                    )
                for ir in item_rows:
                    item_pb = service_pb2.TestItem()
                    item_pb.ParseFromString(ir["proto_bytes"])
                    all_item_pbs.append(item_pb)
            except Exception as e:
                logging.error(f"Failed to load test items for run {run_pb.id}: {e}")
                continue

            sample_item_pbs = self._reconstruct_sample(
                all_item_pbs, completed_pairs, run_pb.sample_size
            )

            cancel_event = asyncio.Event()
            _CANCEL_EVENTS[run_pb.id] = cancel_event

            request = service_pb2.TestRunRequest()
            request.test_id = run_pb.test_id
            request.models.extend(run_pb.models)
            request.include_reasoning = run_pb.include_reasoning
            request.sample_size = run_pb.sample_size
            request.backend_id = run_pb.backend_id

            logging.info(
                f"Resuming stale run {run_pb.id} "
                f"({len(completed_pairs)} answers already completed)"
            )
            asyncio.create_task(
                self._do_run_inference(
                    run_pb,
                    test_pb,
                    sample_item_pbs,
                    request,
                    cancel_event,
                    completed_pairs=completed_pairs,
                ),
                name=f"run-{run_pb.id}",
            )

    async def UpdateTestRun(
        self,
        request: service_pb2.TestRun,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        try:
            original_pb = await self.GetTestRun(
                context=context,
                request=service_pb2.GetRequest(id=request.id),
            )
        except Exception as e:
            logging.error(f"Failed to fetch original test run: {e}")
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Test run {request.id} not found"
            )

        if (request_owner == original_pb.owner) or request_owner_admin:
            request.created_at_utc.CopyFrom(original_pb.created_at_utc)
            request.owner = original_pb.owner
            request.test_id = original_pb.test_id
            request.total_cost_usd = original_pb.total_cost_usd

            if request.status in [
                service_pb2.ResponseCode.SUCCESS,
                service_pb2.ResponseCode.ERROR,
                service_pb2.ResponseCode.CANCELLED,
            ]:
                if not request.completed_at_utc.seconds:
                    request.completed_at_utc.GetCurrentTime()

            response_pb = await self._grpc_update(
                id=request.id, proto_obj=request, obj_type="test_runs"
            )

            # Signal the background inference task to stop if cancellation requested.
            if request.status == service_pb2.ResponseCode.CANCELLED:
                cancel_event = _CANCEL_EVENTS.get(request.id)
                if cancel_event:
                    cancel_event.set()

            return response_pb
        else:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only the run owner or admin can update this test run.",
            )

    async def DeleteTestRun(
        self,
        request: service_pb2.DeleteRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        try:
            original_pb = await self.GetTestRun(
                context=context,
                request=service_pb2.GetRequest(id=request.id),
            )
        except Exception as e:
            logging.error(f"Failed to fetch test run for delete: {e}")
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"TestRun {request.id} not found"
            )

        if (request_owner == original_pb.owner) or request_owner_admin:
            async with self.db_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM test_run_answers WHERE proto_jsonb ->> 'run_id' = $1",
                        request.id,
                    )
                    await conn.execute(
                        "DELETE FROM test_runs WHERE id = $1",
                        request.id,
                    )
            return service_pb2.StatusReply(
                code=service_pb2.ResponseCode.SUCCESS,
                id=request.id,
            )
        else:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only the run owner or admin can delete this test run.",
            )

    async def ListTestRuns(
        self,
        request: service_pb2.ListRequest,
        context: grpc.aio.ServicerContext,
    ):
        try:
            num_items = request.limit if request.limit > 0 else 50
            request_owner = await self.get_request_owner(request)
            is_admin = (
                await self.is_user_admin(user_id=request_owner)
                if request_owner
                else False
            )

            where_clauses: list[str] = []
            query_values: list = []
            if request.parent_id:
                query_values.append(request.parent_id)
                where_clauses.append(
                    f"tr.proto_jsonb->>'test_id' = ${len(query_values)}"
                )
            if request.HasField("created_after"):
                query_values.append(request.created_after.ToDatetime())
                where_clauses.append(f"tr.created_at >= ${len(query_values)}")
            if request.HasField("created_before"):
                query_values.append(request.created_before.ToDatetime())
                where_clauses.append(f"tr.created_at < ${len(query_values)}")

            if not is_admin:
                vis_clause, vis_params = self._visibility_where_clause(
                    requesting_user=request_owner,
                    is_admin=False,
                    param_offset=len(query_values),
                    table_alias="t",
                )
                query_values.extend(vis_params)
                query_values.append(request_owner)
                orphan_clause = f"(t.id IS NULL AND tr.proto_jsonb->>'owner' = ${len(query_values)})"
                where_clauses.append(f"(({vis_clause}) OR {orphan_clause})")

            query_values.append(num_items)

            query_string = (
                "SELECT tr.proto_bytes FROM test_runs tr"
                " LEFT JOIN tests t ON t.id = tr.proto_jsonb->>'test_id'"
            )
            if where_clauses:
                query_string += " WHERE " + " AND ".join(where_clauses)
            query_string += f" ORDER BY tr.created_at DESC LIMIT ${len(query_values)};"

            async with self.db_pool.acquire() as conn:
                run_results = await conn.fetch(query_string, *query_values)
                for run_result in run_results:
                    run_pb = service_pb2.TestRun()
                    run_pb.ParseFromString(run_result["proto_bytes"])
                    yield run_pb

        except Exception as e:
            logging.error(traceback.format_exc())
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )
