import grpc
import uuid

from absl import logging

from server import service_pb2
from server.objects.backend_pool import (
    invalidate_backend_resources,
)


class BackendsMixin:
    async def _list_enabled_backends(self, limit: int) -> list[bytes]:
        """Returns proto_bytes for enabled backends only. Override in tests."""
        if not self.db_pool:
            return []
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT proto_bytes FROM backends
                       WHERE proto_jsonb ->> 'enabled' = 'true'
                       ORDER BY created_at DESC LIMIT $1""",
                    limit,
                )
                return [r["proto_bytes"] for r in rows]
        except Exception as e:
            logging.error(f"Failed to list enabled backends: {e}")
            return []

    async def CreateBackend(
        self,
        request: service_pb2.Backend,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request=request)
        if not await self.is_user_admin(user_id=request_owner):
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only admins can create backends.",
            )
        request.id = uuid.uuid4().hex
        request.owner = request_owner
        request.enabled = True  # new backends are enabled by default
        request.created_at_utc.GetCurrentTime()
        request.modified_at_utc.GetCurrentTime()
        return await self._grpc_create(
            id=request.id, proto_obj=request, obj_type="backends"
        )

    async def GetBackend(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.Backend:
        proto_bytes = await self._grpc_get(id=request.id, obj_type="backends")
        if not proto_bytes:
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Backend {request.id} not found"
            )
        backend_pb = service_pb2.Backend()
        backend_pb.ParseFromString(proto_bytes)
        backend_pb.api_key = ""  # never return the secret
        return backend_pb

    async def UpdateBackend(
        self,
        request: service_pb2.Backend,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request=request)
        if not await self.is_user_admin(user_id=request_owner):
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only admins can update backends.",
            )

        proto_bytes = await self._grpc_get(id=request.id, obj_type="backends")
        if not proto_bytes:
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Backend {request.id} not found"
            )
        stored_pb = service_pb2.Backend()
        stored_pb.ParseFromString(proto_bytes)

        request.modified_at_utc.GetCurrentTime()
        request.created_at_utc.CopyFrom(stored_pb.created_at_utc)
        request.owner = stored_pb.owner

        # Preserve existing api_key if caller sends empty string
        if not request.api_key:
            request.api_key = stored_pb.api_key

        await invalidate_backend_resources(request.id)
        return await self._grpc_update(
            id=request.id, proto_obj=request, obj_type="backends"
        )

    async def DeleteBackend(
        self,
        request: service_pb2.DeleteRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request=request)
        if not await self.is_user_admin(user_id=request_owner):
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only admins can delete backends.",
            )
        await invalidate_backend_resources(request.id)
        return await self._grpc_delete(
            id=request.id, obj_type="backends", obj_owner=request_owner
        )

    async def ListBackends(
        self,
        request: service_pb2.ListRequest,
        context: grpc.aio.ServicerContext,
    ):
        request_owner = await self.get_request_owner(request=request)
        is_admin = await self.is_user_admin(user_id=request_owner)
        limit = request.limit if request.limit > 0 else 1000

        if is_admin:
            # Admins see all backends regardless of enabled state
            results = await self._grpc_list(obj_type="backends", limit=limit)
        else:
            # Regular users only see enabled backends
            results = await self._list_enabled_backends(limit=limit)

        for proto_bytes in results:
            backend_pb = service_pb2.Backend()
            backend_pb.ParseFromString(proto_bytes)
            backend_pb.api_key = ""  # never return the secret
            yield backend_pb
