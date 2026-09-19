import asyncio
import time

import grpc
import uuid

from absl import logging

from server import service_pb2


class TestsMixin:
    async def CreateTest(
        self,
        request: service_pb2.Test,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request=request)
        request.id = uuid.uuid4().hex
        request.owner = request_owner
        request.created_at_utc.GetCurrentTime()
        request.modified_at_utc.GetCurrentTime()
        response_pb = await self._grpc_create(
            id=request.id,
            proto_obj=request,
            obj_type="tests",
        )

        return response_pb

    async def GetTest(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.Test:
        try:
            request_owner = await self.get_request_owner(request)
            is_admin = (
                await self.is_user_admin(user_id=request_owner)
                if request_owner
                else False
            )

            async with self.db_pool.acquire() as conn:
                if is_admin:
                    row = await conn.fetchrow(
                        "SELECT proto_bytes, item_count FROM tests WHERE id = $1",
                        request.id,
                    )
                else:
                    vis_clause, vis_params = self._visibility_where_clause(
                        requesting_user=request_owner,
                        is_admin=False,
                        param_offset=1,
                    )
                    row = await conn.fetchrow(
                        f"SELECT proto_bytes, item_count FROM tests WHERE id = $1 AND {vis_clause}",
                        request.id,
                        *vis_params,
                    )

            if not row or not row["proto_bytes"]:
                raise ValueError("No test found")

            test_pb = service_pb2.Test()
            test_pb.ParseFromString(row["proto_bytes"])
            test_pb.item_count = row["item_count"]
            return test_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )

    async def UpdateTest(
        self,
        request: service_pb2.Test,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        try:
            original_pb = await self.GetTest(
                context=context,
                request=service_pb2.GetRequest(id=request.id),
            )
        except Exception as e:
            logging.error(f"Failed to fetch original test: {e}")
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Test {request.id} not found"
            )

        if (request_owner == original_pb.owner) or request_owner_admin:
            request.modified_at_utc.GetCurrentTime()
            request.created_at_utc.CopyFrom(original_pb.created_at_utc)
            request.owner = original_pb.owner

            response_pb = await self._grpc_update(
                id=request.id, proto_obj=request, obj_type="tests"
            )

            return response_pb
        else:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only the test owner or admin can update this test.",
            )

    async def DeleteTest(
        self,
        request: service_pb2.DeleteRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        try:
            original_pb = await self.GetTest(
                context=context,
                request=service_pb2.GetRequest(id=request.id),
            )
        except Exception as e:
            logging.error(f"Failed to fetch test for delete: {e}")
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Test {request.id} not found"
            )

        if (request_owner == original_pb.owner) or request_owner_admin:
            async with self.db_pool.acquire() as conn:
                async with conn.transaction():
                    # Attachments exclusively referenced by this test's items.
                    exclusive_att_ids = [
                        row["att_id"]
                        for row in await conn.fetch(
                            """
                            SELECT DISTINCT proto_jsonb ->> 'attachment_id' AS att_id
                            FROM test_items
                            WHERE proto_jsonb ->> 'test_id' = $1
                              AND proto_jsonb ->> 'attachment_id' != ''
                              AND NOT EXISTS (
                                SELECT 1 FROM test_items t2
                                WHERE t2.proto_jsonb ->> 'attachment_id'
                                      = test_items.proto_jsonb ->> 'attachment_id'
                                  AND t2.proto_jsonb ->> 'test_id' != $1
                              )
                            """,
                            request.id,
                        )
                    ]

                    await conn.execute(
                        "DELETE FROM test_items WHERE proto_jsonb ->> 'test_id' = $1",
                        request.id,
                    )

                    if exclusive_att_ids:
                        await conn.execute(
                            "DELETE FROM attachments WHERE id = ANY($1)",
                            exclusive_att_ids,
                        )

                    await conn.execute(
                        "DELETE FROM tests WHERE id = $1",
                        request.id,
                    )

            return service_pb2.StatusReply(
                code=service_pb2.ResponseCode.SUCCESS,
                id=request.id,
            )
        else:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only the test owner or admin can delete this test.",
            )

    async def ListTests(
        self,
        request: service_pb2.ListRequest,
        context: grpc.aio.ServicerContext,
    ):
        limit = request.limit if request.limit > 0 else 50
        request_owner = await self.get_request_owner(request)
        is_admin = (
            await self.is_user_admin(user_id=request_owner) if request_owner else False
        )

        async with self.db_pool.acquire() as conn:
            if is_admin:
                rows = await conn.fetch(
                    "SELECT proto_bytes, item_count FROM tests ORDER BY created_at DESC LIMIT $1",
                    limit,
                )
            else:
                vis_clause, vis_params = self._visibility_where_clause(
                    requesting_user=request_owner,
                    is_admin=False,
                    param_offset=0,
                )
                limit_param = f"${len(vis_params) + 1}"
                rows = await conn.fetch(
                    f"SELECT proto_bytes, item_count FROM tests WHERE {vis_clause} ORDER BY created_at DESC LIMIT {limit_param}",
                    *vis_params,
                    limit,
                )
        for row in rows:
            test_pb = service_pb2.Test()
            test_pb.ParseFromString(row["proto_bytes"])
            test_pb.item_count = row["item_count"]
            yield test_pb
