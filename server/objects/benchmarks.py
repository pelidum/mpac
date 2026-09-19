import asyncio
import grpc
import hashlib
import math
import random
import time
import traceback
import uuid

from absl import logging
from google.protobuf import timestamp_pb2

from server import service_pb2

_MATCH_RUN_TIMEOUT = 1800  # 30 min max per match run
_STALE_THRESHOLD_S = 600  # 10 min without progress → considered orphaned


class _BenchmarkContext:
    """Minimal context stand-in for internal calls made from background tasks."""

    def cancelled(self) -> bool:
        return False

    async def abort(self, code, message):
        raise RuntimeError(f"gRPC abort ({code}): {message}")


def _advisory_lock_key(benchmark_id: str) -> int:
    """Stable bigint key for pg_try_advisory_lock across processes.

    Python's hash() is seed-randomized per process, so two instances would
    derive different keys for the same benchmark and the lock would never
    collide. A truncated SHA-256 digest is stable everywhere.
    """
    digest = hashlib.sha256(benchmark_id.encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _reconstruct_bracket(benchmark_pb: service_pb2.Benchmark):
    """Reconstruct tournament state from persisted match_history.

    Returns (active_competitors, round_number, prior_winners) where
    prior_winners contains winners already determined in a partially
    completed current round (empty if the last round was fully complete).
    """
    all_model_ids = [m.id for m in benchmark_pb.models]

    if not benchmark_pb.match_history:
        random.shuffle(all_model_ids)
        return all_model_ids, 1, []

    last_round = max(m.round for m in benchmark_pb.match_history)

    # Determine who entered this round
    if last_round == 1:
        round_entrants = all_model_ids
    else:
        round_entrants = [
            m.winner for m in benchmark_pb.match_history if m.round == last_round - 1
        ]

    # Who already competed and won in the current round
    competed = set()
    round_winners = []
    for m in benchmark_pb.match_history:
        if m.round == last_round:
            for r in m.responses:
                competed.add(r.responder)
            round_winners.append(m.winner)

    remaining = [mid for mid in round_entrants if mid not in competed]

    if not remaining:
        # Round fully complete — advance to next
        random.shuffle(round_winners)
        return round_winners, last_round + 1, []
    else:
        # Partial round — finish it
        random.shuffle(remaining)
        return remaining, last_round, round_winners


class BenchmarksMixin:
    async def GetBenchmark(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.Benchmark:
        try:
            benchmark_pb = service_pb2.Benchmark()
            benchmark_bytes = await self._grpc_get(
                id=request.id,
                obj_type="benchmarks",
            )
            if benchmark_bytes:
                benchmark_pb.ParseFromString(benchmark_bytes)
            else:
                return benchmark_pb

            if benchmark_pb.test_id:
                parent_test = await self._grpc_get(
                    id=benchmark_pb.test_id, obj_type="tests"
                )
                if not parent_test:
                    await context.abort(
                        grpc.StatusCode.NOT_FOUND,
                        "Benchmark not found.",
                    )

            return benchmark_pb
        except Exception as e:
            logging.error(e)
            await context.abort(grpc.StatusCode.ABORTED, "An internal error occurred")

    async def CreateBenchmark(
        self,
        request: service_pb2.BenchmarkRequest,
        context: grpc.aio.ServicerContext,
    ):
        request_owner = await self.get_request_owner(request)
        if not request_owner:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED, "No credentials provided."
            )

        await self.check_spend_limits(user_id=request_owner, context=context)
        projected = await self._projected_benchmark_cost(
            request.models,
            request.sample_size or 25,
            include_reasoning=request.include_reasoning,
        )
        await self.check_projected_cost(
            request_owner, projected, context, label="benchmark"
        )

        benchmark_id = uuid.uuid4().hex
        benchmark_pb = service_pb2.Benchmark()
        benchmark_pb.id = benchmark_id
        benchmark_pb.created_at_utc.GetCurrentTime()
        benchmark_pb.status = service_pb2.ResponseCode.IN_PROGRESS
        benchmark_pb.owner = request_owner
        benchmark_pb.test_id = request.test_id

        try:
            test_pb = await self.GetTest(
                service_pb2.GetRequest(id=benchmark_pb.test_id),
                context=context,
            )
            if not test_pb.type == 1:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Benchmarks require tests of type: EVALUATION",
                )

            n = len(request.models)
            num_rounds = max(1, math.ceil(math.log2(n))) if n >= 2 else 0
            benchmark_pb.model_count = n
            benchmark_pb.models.extend(request.models)
            benchmark_pb.sample_size = request.sample_size or 25
            benchmark_pb.include_reasoning = request.include_reasoning

            await self._grpc_create(
                id=benchmark_id,
                proto_obj=benchmark_pb,
                obj_type="benchmarks",
                overwrite=False,
            )

            asyncio.create_task(
                self._do_benchmark(benchmark_pb),
                name=f"benchmark-{benchmark_id}",
            )

            resp = service_pb2.BenchmarkResponse()
            resp.benchmark_id = benchmark_id
            resp.code = service_pb2.ResponseCode.IN_PROGRESS
            resp.total_rounds = num_rounds
            yield resp

        except Exception as e:
            logging.error(e)
            await context.abort(grpc.StatusCode.ABORTED, "An internal error occurred")

    async def _do_benchmark(
        self,
        benchmark_pb: service_pb2.Benchmark,
    ) -> None:
        benchmark_id = benchmark_pb.id
        _ctx = _BenchmarkContext()

        active_competitors, round_number, prior_winners = _reconstruct_bracket(
            benchmark_pb
        )

        n = len(benchmark_pb.models)
        num_rounds = max(1, math.ceil(math.log2(n))) if n >= 2 else 0
        match_history = list(benchmark_pb.match_history)
        total_cost_usd = benchmark_pb.total_cost_usd

        if match_history:
            logging.info(
                f"Resuming benchmark {benchmark_id} at round {round_number} "
                f"with {len(active_competitors)} competitors "
                f"({len(prior_winners)} matches already done this round)"
            )

        try:
            while len(active_competitors) > 1 or prior_winners:
                next_round_winners = list(prior_winners)
                prior_winners = []

                for i in range(0, len(active_competitors), 2):
                    if i + 1 < len(active_competitors):
                        model1_id = active_competitors[i]
                        model2_id = active_competitors[i + 1]
                        match_models = [
                            m
                            for m in benchmark_pb.models
                            if m.id in {model1_id, model2_id}
                        ]

                        match_request = service_pb2.TestRunRequest(
                            test_id=benchmark_pb.test_id,
                            include_reasoning=benchmark_pb.include_reasoning,
                            sample_size=benchmark_pb.sample_size,
                        )
                        match_request.models.extend(match_models)
                        match_request.labels.append(f"benchmark-{benchmark_id}")

                        run_id = ""
                        async for reply in self.CreateTestRun(
                            request=match_request,
                            context=_ctx,
                        ):
                            if reply.run_id:
                                run_id = reply.run_id

                        final_metrics: dict = {}
                        if run_id:
                            poll_interval = 0.5
                            poll_start = time.monotonic()
                            while True:
                                await asyncio.sleep(poll_interval)
                                poll_interval = min(poll_interval * 1.5, 5.0)
                                run_pb = await self.GetTestRun(
                                    request=service_pb2.GetRequest(id=run_id),
                                    context=_ctx,
                                )
                                if (
                                    run_pb.status
                                    != service_pb2.ResponseCode.IN_PROGRESS
                                ):
                                    final_metrics = {
                                        m.responder_id: m
                                        for m in run_pb.metrics
                                        if m.responder_id in {model1_id, model2_id}
                                    }
                                    total_cost_usd += run_pb.total_cost_usd
                                    break
                                if time.monotonic() - poll_start > _MATCH_RUN_TIMEOUT:
                                    logging.warning(
                                        f"Benchmark {benchmark_id}: match run {run_id} "
                                        f"timed out after {_MATCH_RUN_TIMEOUT}s"
                                    )
                                    break

                        m1 = final_metrics.get(model1_id)
                        m2 = final_metrics.get(model2_id)
                        m1_f1 = m1.f1 if m1 else -1.0
                        m2_f1 = m2.f1 if m2 else -1.0
                        m1_cmp = m1_f1 if m1_f1 >= 0 else -1.0
                        m2_cmp = m2_f1 if m2_f1 >= 0 else -1.0
                        if m1_cmp == m2_cmp:
                            winner_id = random.choice([model1_id, model2_id])
                        elif m1_cmp > m2_cmp:
                            winner_id = model1_id
                        else:
                            winner_id = model2_id

                        next_round_winners.append(winner_id)

                        match_pb = service_pb2.BenchmarkMatch()
                        match_pb.round = round_number
                        match_pb.id = (
                            len([x for x in match_history if x.round == round_number])
                            + 1
                        )
                        match_pb.winner = winner_id

                        r1 = service_pb2.BenchmarkMatchResponse()
                        r1.responder = model1_id
                        r1.run_id = run_id
                        r1.precision = m1.precision if m1 else -1.0
                        r1.recall = m1.recall if m1 else -1.0
                        r1.f1 = m1_f1
                        match_pb.responses.append(r1)

                        r2 = service_pb2.BenchmarkMatchResponse()
                        r2.responder = model2_id
                        r2.run_id = run_id
                        r2.precision = m2.precision if m2 else -1.0
                        r2.recall = m2.recall if m2 else -1.0
                        r2.f1 = m2_f1
                        match_pb.responses.append(r2)

                    else:
                        bye_model_id = active_competitors[i]
                        next_round_winners.append(bye_model_id)

                        match_pb = service_pb2.BenchmarkMatch()
                        match_pb.round = round_number
                        match_pb.id = (
                            len([x for x in match_history if x.round == round_number])
                            + 1
                        )
                        match_pb.winner = bye_model_id

                        bye_resp = service_pb2.BenchmarkMatchResponse()
                        bye_resp.responder = bye_model_id
                        match_pb.responses.append(bye_resp)

                    match_history.append(match_pb)
                    benchmark_pb.match_history.extend([match_pb])
                    await self._grpc_update(
                        id=benchmark_id,
                        proto_obj=benchmark_pb,
                        obj_type="benchmarks",
                    )

                active_competitors = next_round_winners
                round_number += 1

            final_winner = active_competitors[0] if active_competitors else ""
            benchmark_pb.winner = final_winner
            benchmark_pb.status = service_pb2.ResponseCode.SUCCESS
            benchmark_pb.total_cost_usd = total_cost_usd
            benchmark_pb.completed_at_utc.GetCurrentTime()
            await self._grpc_update(
                id=benchmark_id,
                proto_obj=benchmark_pb,
                obj_type="benchmarks",
            )

        except asyncio.CancelledError:
            logging.info(f"Benchmark {benchmark_id} cancelled")
            benchmark_pb.status = service_pb2.ResponseCode.CANCELLED
            benchmark_pb.total_cost_usd = total_cost_usd
            benchmark_pb.completed_at_utc.GetCurrentTime()
            await self._grpc_update(
                id=benchmark_id,
                proto_obj=benchmark_pb,
                obj_type="benchmarks",
            )

        except Exception as e:
            logging.error(
                f"Benchmark {benchmark_id} failed: {e}\n{traceback.format_exc()}"
            )
            benchmark_pb.status = service_pb2.ResponseCode.ERROR
            benchmark_pb.completed_at_utc.GetCurrentTime()
            await self._grpc_update(
                id=benchmark_id,
                proto_obj=benchmark_pb,
                obj_type="benchmarks",
            )

    async def _is_benchmark_stale(self, benchmark_pb: service_pb2.Benchmark) -> bool:
        """Check if a benchmark's background task is likely dead."""
        if not benchmark_pb.match_history:
            # No matches started — stale if created > threshold ago
            age = time.time() - benchmark_pb.created_at_utc.ToSeconds()
            return age > _STALE_THRESHOLD_S

        # Find the most recent match run
        last_run_id = ""
        for match in benchmark_pb.match_history:
            for resp in match.responses:
                if resp.run_id:
                    last_run_id = resp.run_id

        if not last_run_id:
            return True

        _ctx = _BenchmarkContext()
        run_pb = await self.GetTestRun(
            request=service_pb2.GetRequest(id=last_run_id),
            context=_ctx,
        )

        if run_pb.status == service_pb2.ResponseCode.IN_PROGRESS:
            return False

        if run_pb.completed_at_utc.seconds:
            age = time.time() - run_pb.completed_at_utc.ToSeconds()
            return age > _STALE_THRESHOLD_S

        return True

    async def resume_stale_benchmarks(self) -> None:
        """Detect and resume benchmarks orphaned by instance recycling.

        Called once at server startup. Uses pg_try_advisory_lock to prevent
        two instances from resuming the same benchmark concurrently.
        """
        if not self.db_pool:
            return

        try:
            rows = await self.db_pool.fetch(
                """SELECT id, proto_bytes FROM benchmarks
                   WHERE proto_jsonb->>'status' = 'IN_PROGRESS'"""
            )
        except Exception as e:
            logging.error(f"Failed to query for stale benchmarks: {e}")
            return

        for row in rows:
            benchmark_pb = service_pb2.Benchmark()
            benchmark_pb.ParseFromString(row["proto_bytes"])

            if not benchmark_pb.models:
                logging.warning(
                    f"Benchmark {benchmark_pb.id} has no stored models "
                    f"(pre-migration), marking as ERROR"
                )
                benchmark_pb.status = service_pb2.ResponseCode.ERROR
                benchmark_pb.completed_at_utc.GetCurrentTime()
                await self._grpc_update(
                    id=benchmark_pb.id,
                    proto_obj=benchmark_pb,
                    obj_type="benchmarks",
                )
                continue

            if not await self._is_benchmark_stale(benchmark_pb):
                continue

            lock_key = _advisory_lock_key(benchmark_pb.id)
            try:
                async with self.db_pool.acquire() as conn:
                    locked = await conn.fetchval(
                        "SELECT pg_try_advisory_lock($1)", lock_key
                    )
                if not locked:
                    logging.info(
                        f"Benchmark {benchmark_pb.id} locked by another instance"
                    )
                    continue
            except Exception as e:
                logging.error(f"Advisory lock failed for {benchmark_pb.id}: {e}")
                continue

            logging.info(f"Resuming stale benchmark {benchmark_pb.id}")
            asyncio.create_task(
                self._do_benchmark(benchmark_pb),
                name=f"benchmark-{benchmark_pb.id}",
            )

    async def UpdateBenchmark(
        self,
        request: service_pb2.Benchmark,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        try:
            original_pb = await self.GetBenchmark(
                context=context,
                request=service_pb2.GetRequest(id=request.id),
            )
        except Exception as e:
            logging.error(f"Failed to fetch original benchmark: {e}")
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Benchmark {request.id} not found"
            )

        if (request_owner == original_pb.owner) or request_owner_admin:
            request.created_at_utc.CopyFrom(original_pb.created_at_utc)

            if request.status in [
                service_pb2.ResponseCode.SUCCESS,
                service_pb2.ResponseCode.ERROR,
                service_pb2.ResponseCode.CANCELLED,
            ]:
                if not request.completed_at_utc.seconds:
                    request.completed_at_utc.GetCurrentTime()

            response_pb = await self._grpc_update(
                id=request.id, proto_obj=request, obj_type="benchmarks"
            )

            if request.status == service_pb2.ResponseCode.CANCELLED:
                task_name = f"benchmark-{request.id}"
                for task in asyncio.all_tasks():
                    if task.get_name() == task_name:
                        task.cancel()
                        break

            return response_pb
        else:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only the benchmark owner or admin can update this benchmark.",
            )

    async def DeleteBenchmark(
        self,
        request: service_pb2.DeleteRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        try:
            request_owner = await self.get_request_owner(request)
            response_pb = await self._grpc_delete(
                id=request.id,
                obj_type="benchmarks",
                obj_owner=request_owner,
            )
            return response_pb
        except Exception as e:
            logging.error(e)
            await context.abort(grpc.StatusCode.ABORTED, "An internal error occurred")

    async def ListBenchmarks(
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

            if not is_admin:
                vis_clause, vis_params = self._visibility_where_clause(
                    requesting_user=request_owner,
                    is_admin=False,
                    param_offset=len(query_values),
                    table_alias="t",
                )
                where_clauses.append(vis_clause)
                query_values.extend(vis_params)

            query_values.append(num_items)

            query_string = (
                "SELECT b.proto_bytes FROM benchmarks b"
                " JOIN tests t ON t.id = b.proto_jsonb->>'test_id'"
            )
            if where_clauses:
                query_string += " WHERE " + " AND ".join(where_clauses)
            query_string += f" ORDER BY b.created_at DESC LIMIT ${len(query_values)};"

            async with self.db_pool.acquire() as conn:
                results = await conn.fetch(query_string, *query_values)
                for row in results:
                    benchmark_pb = service_pb2.Benchmark()
                    benchmark_pb.ParseFromString(row["proto_bytes"])
                    yield benchmark_pb

        except Exception as e:
            logging.error(e)
            await context.abort(grpc.StatusCode.ABORTED, "An internal error occurred")
