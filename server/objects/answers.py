import grpc

from absl import logging

from server import service_pb2


class AnswersMixin:
    async def ListTestRunAnswers(
        self,
        request: service_pb2.ListRequest,
        context: grpc.aio.ServicerContext,
    ):
        try:
            request_owner = await self.get_request_owner(request)
            is_admin = (
                await self.is_user_admin(user_id=request_owner)
                if request_owner
                else False
            )
            if not is_admin:
                run_bytes = await self._grpc_get(
                    id=request.parent_id,
                    obj_type="test_runs",
                )
                if run_bytes is None:
                    return

                run_pb = service_pb2.TestRun()
                run_pb.ParseFromString(run_bytes)
                if run_pb.test_id:
                    parent_test = await self._grpc_get(
                        id=run_pb.test_id, obj_type="tests"
                    )
                    if not parent_test and run_pb.owner != request_owner:
                        return

            async with self.db_pool.acquire() as conn:
                num_items = request.limit
                if request.limit == 0:
                    num_items = 50

                query_string = """
                    with item_ids as (
                        SELECT
                            proto_jsonb ->> 'item_id' as item_id
                        FROM test_run_answers
                        WHERE proto_jsonb ->> 'run_id' = $1
                        GROUP BY 1
                        ORDER BY 1
                        LIMIT $2
                    )
                    SELECT proto_bytes FROM test_run_answers WHERE proto_jsonb ->> 'item_id' IN (SELECT item_id FROM item_ids) AND proto_jsonb ->> 'run_id' = $1
                    """
                query_values = (request.parent_id, num_items)
                answer_results = await conn.fetch(query_string, *query_values)
                if answer_results:
                    for answer_result in answer_results:
                        answer_pb = service_pb2.TestRunAnswer()
                        answer_pb.ParseFromString(answer_result["proto_bytes"])
                        yield answer_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )
