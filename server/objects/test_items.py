import grpc
import uuid

from absl import logging
from google.protobuf.json_format import MessageToJson

from server import service_pb2


class TestItemsMixin:
    async def CreateTestItem(
        self,
        request: service_pb2.TestItem,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        test_pb = await self.GetTest(
            request=service_pb2.GetRequest(id=request.test_id),
            context=context,
        )

        request_owner = await self.get_request_owner(request=request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)
        if not ((test_pb.owner == request_owner) or request_owner_admin):
            return service_pb2.StatusReply(
                code=service_pb2.ResponseCode.ERROR,
                reason="User does not have permission to add items to this test.",
                id=request.test_id,
            )

        request.id = uuid.uuid4().hex
        response_pb = await self._grpc_create(
            id=request.id,
            proto_obj=request,
            obj_type="test_items",
        )

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE tests SET item_count = item_count + 1 WHERE id = $1",
                request.test_id,
            )

        return response_pb

    async def BatchCreateTestItems(
        self,
        request: service_pb2.BatchCreateTestItemsRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.BatchCreateTestItemsReply:
        request_owner = await self.get_request_owner(request=request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        items_by_test = {}
        for item in request.items:
            if item.test_id not in items_by_test:
                items_by_test[item.test_id] = []
            items_by_test[item.test_id].append(item)

        test_permissions = {}
        for test_id in items_by_test.keys():
            try:
                test_pb = await self.GetTest(
                    request=service_pb2.GetRequest(id=test_id),
                    context=context,
                )
                test_permissions[test_id] = (
                    test_pb.owner == request_owner
                ) or request_owner_admin
            except Exception:
                test_permissions[test_id] = False

        results = []
        successful_count = 0
        failed_count = 0

        # Separate permitted items from denied ones up front.
        permitted_items = []
        for item in request.items:
            if not test_permissions.get(item.test_id, False):
                results.append(
                    service_pb2.StatusReply(
                        code=service_pb2.ResponseCode.ERROR,
                        reason=f"User does not have permission to add items to test {item.test_id}.",
                        id=item.test_id,
                    )
                )
                failed_count += 1
            else:
                item.id = uuid.uuid4().hex
                permitted_items.append(item)

        # Bulk-insert all permitted items in a single DB round-trip.
        if permitted_items:
            rows = []
            for item in permitted_items:
                proto_json = MessageToJson(
                    message=item,
                    always_print_fields_with_no_presence=True,
                    preserving_proto_field_name=True,
                    indent=0,
                )
                rows.append((item.id, proto_json, item.SerializeToString()))

            try:
                async with self.db_pool.acquire() as conn:
                    await conn.executemany(
                        "INSERT INTO test_items (id, proto_jsonb, proto_bytes) VALUES ($1, $2, $3)",
                        rows,
                    )
                    counts_by_test = {}
                    for item in permitted_items:
                        counts_by_test[item.test_id] = (
                            counts_by_test.get(item.test_id, 0) + 1
                        )
                    for test_id, delta in counts_by_test.items():
                        await conn.execute(
                            "UPDATE tests SET item_count = item_count + $1 WHERE id = $2",
                            delta,
                            test_id,
                        )
                for item in permitted_items:
                    results.append(
                        service_pb2.StatusReply(
                            code=service_pb2.ResponseCode.SUCCESS,
                            id=item.id,
                        )
                    )
                    successful_count += 1
            except Exception as e:
                logging.error(f"Bulk insert failed: {e}")
                for item in permitted_items:
                    results.append(
                        service_pb2.StatusReply(
                            code=service_pb2.ResponseCode.ERROR,
                            reason="An internal error occurred",
                            id=item.id,
                        )
                    )
                    failed_count += 1

        return service_pb2.BatchCreateTestItemsReply(
            results=results,
            successful_count=successful_count,
            failed_count=failed_count,
        )

    async def GetTestItem(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.TestItem:
        try:
            test_item_pb = service_pb2.TestItem()
            test_item_bytes = await self._grpc_get(
                id=request.id,
                obj_type="test_items",
            )
            if test_item_bytes:
                test_item_pb.ParseFromString(test_item_bytes)

                request_owner = await self.get_request_owner(request)
                is_admin = (
                    await self.is_user_admin(user_id=request_owner)
                    if request_owner
                    else False
                )
                if not is_admin:
                    test_bytes = await self._grpc_get(
                        id=test_item_pb.test_id,
                        obj_type="tests",
                    )
                    if test_bytes is None:
                        return service_pb2.TestItem()

            return test_item_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )

    async def UpdateTestItem(
        self,
        request: service_pb2.TestItem,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        try:
            test_pb = await self.GetTest(
                request=service_pb2.GetRequest(id=request.test_id),
                context=context,
            )
        except Exception as e:
            logging.error(f"Failed to fetch test for item update: {e}")
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Test {request.test_id} not found"
            )

        if (test_pb.owner == request_owner) or request_owner_admin:
            response_pb = await self._grpc_update(
                id=request.id,
                proto_obj=request,
                obj_type="test_items",
            )
            return response_pb
        else:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only the test owner or admin can update items in this test.",
            )

    async def DeleteTestItem(
        self,
        request: service_pb2.DeleteRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        try:
            item_pb = await self.GetTestItem(
                request=service_pb2.GetRequest(id=request.id),
                context=context,
            )
        except Exception as e:
            logging.error(f"Failed to fetch test item for delete: {e}")
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"TestItem {request.id} not found"
            )

        try:
            test_pb = await self.GetTest(
                request=service_pb2.GetRequest(id=item_pb.test_id),
                context=context,
            )
        except Exception as e:
            logging.error(f"Failed to fetch parent test for item delete: {e}")
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Test {item_pb.test_id} not found"
            )

        if (test_pb.owner == request_owner) or request_owner_admin:
            response_pb = await self._grpc_delete(
                id=request.id,
                obj_type="test_items",
                obj_owner=request_owner,
            )
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE tests SET item_count = GREATEST(0, item_count - 1) WHERE id = $1",
                    item_pb.test_id,
                )
            return response_pb
        else:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only the test owner or admin can delete items from this test.",
            )

    async def ListTestItems(
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
                test_bytes = await self._grpc_get(
                    id=request.parent_id,
                    obj_type="tests",
                )
                if test_bytes is None:
                    return

            async with self.db_pool.acquire() as conn:
                query_string = """
                    SELECT proto_bytes
                    FROM test_items
                    WHERE proto_jsonb ->> 'test_id' = $1
                    ORDER BY created_at
                    LIMIT $2;
                    """

                limit = request.limit if request.limit else 50
                query_values = (request.parent_id, limit)
                test_item_results = await conn.fetch(query_string, *query_values)
                if test_item_results:
                    for test_item_result in test_item_results:
                        test_item_pb = service_pb2.TestItem()
                        test_item_pb.ParseFromString(test_item_result["proto_bytes"])
                        yield test_item_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )
