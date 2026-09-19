import grpc
import os
import tempfile

from absl import logging

from server import service_pb2


class AttachmentsMixin:
    async def GetAttachment(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.FileAttachment:
        try:
            attachment_pb = service_pb2.FileAttachment()
            attachment_bytes = await self._grpc_get(
                id=request.id,
                obj_type="attachments",
            )
            if attachment_bytes:
                attachment_pb.ParseFromString(attachment_bytes)

            return attachment_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )

    async def BatchGetAttachments(
        self,
        request: service_pb2.BatchGetAttachmentsRequest,
        context: grpc.aio.ServicerContext,
    ):
        try:
            request_owner = await self.get_request_owner(request)
            is_admin = (
                await self.is_user_admin(user_id=request_owner)
                if request_owner
                else False
            )

            async with self.db_pool.acquire() as conn:
                if is_admin:
                    rows = await conn.fetch(
                        "SELECT proto_bytes FROM attachments WHERE id = ANY($1::text[])",
                        list(request.ids),
                    )
                else:
                    vis_clause, vis_params = self._visibility_where_clause(
                        requesting_user=request_owner,
                        is_admin=False,
                        param_offset=1,
                    )
                    rows = await conn.fetch(
                        f"SELECT proto_bytes FROM attachments WHERE id = ANY($1::text[]) AND {vis_clause}",
                        list(request.ids),
                        *vis_params,
                    )
            for row in rows:
                attachment_pb = service_pb2.FileAttachment()
                attachment_pb.ParseFromString(row["proto_bytes"])
                if request.metadata_only:
                    attachment_pb.ClearField("file")
                yield attachment_pb
        except Exception as e:
            logging.error(e)
            await context.abort(grpc.StatusCode.ABORTED, "An internal error occurred")

    async def CreateAttachment(
        self,
        request: service_pb2.FileAttachment,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        try:
            if not request.name:
                return service_pb2.StatusReply(
                    code=service_pb2.ResponseCode.ERROR,
                    reason="Attachment name is required",
                )

            if not request.file:
                return service_pb2.StatusReply(
                    code=service_pb2.ResponseCode.ERROR,
                    reason="File data is required",
                )

            request_owner = await self.get_request_owner(request=request)
            request.owner = request_owner
            max_size = 100 * 1024 * 1024  # 100 MB
            actual_size = len(request.file)
            if actual_size > max_size:
                return service_pb2.StatusReply(
                    code=service_pb2.ResponseCode.ERROR,
                    reason=f"File size {actual_size} bytes exceeds maximum of {max_size} bytes",
                    id="",
                )

            temp_file = None
            try:
                with tempfile.NamedTemporaryFile(
                    delete=False, suffix=f".{request.extension}"
                ) as tf:
                    tf.write(request.file)
                    temp_file = tf.name

                parsed_attachment = self.attachment_parser(
                    file_name=request.name,
                    file_path=temp_file,
                    owner=request.owner,
                )
                if (
                    parsed_attachment.modality
                    == service_pb2.FileModality.UNSPECIFIED_MODALITY
                ):
                    return service_pb2.StatusReply(
                        code=service_pb2.ResponseCode.ERROR,
                        reason="Unsupported file type. Only audio, image, video, and text files are supported.",
                        id="",
                    )

                response_pb = await self._grpc_create(
                    id=parsed_attachment.id,
                    proto_obj=parsed_attachment,
                    obj_type="attachments",
                )

                if response_pb.code == service_pb2.ResponseCode.SUCCESS:
                    return service_pb2.StatusReply(
                        code=service_pb2.ResponseCode.SUCCESS,
                        reason=f"Attachment '{parsed_attachment.name}' created successfully",
                        id=parsed_attachment.id,
                    )
                else:
                    return response_pb

            finally:
                if temp_file and os.path.exists(temp_file):
                    os.unlink(temp_file)

        except Exception as e:
            logging.error(f"Error creating attachment {request.name}: {e}")
            return service_pb2.StatusReply(
                code=service_pb2.ResponseCode.ERROR,
                reason="Failed to create attachment",
                id="",
            )

    async def DeleteAttachment(
        self,
        request: service_pb2.DeleteRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.StatusReply:
        request_owner = await self.get_request_owner(request)
        request_owner_admin = await self.is_user_admin(user_id=request_owner)

        try:
            original_pb = await self.GetAttachment(
                request=service_pb2.GetRequest(id=request.id),
                context=context,
            )
        except Exception as e:
            logging.error(f"Failed to fetch attachment for delete: {e}")
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"Attachment {request.id} not found"
            )

        if (request_owner == original_pb.owner) or request_owner_admin:
            response_pb = await self._grpc_delete(
                id=request.id,
                obj_type="attachments",
                obj_owner=request_owner,
            )
            return response_pb
        else:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Only the attachment owner or admin can delete this attachment.",
            )

    async def ListAttachments(
        self,
        request: service_pb2.ListRequest,
        context: grpc.aio.ServicerContext,
    ):
        try:
            attachment_bytes_results = await self._grpc_list(
                obj_type="attachments",
                limit=request.limit,
            )
            for attachment_bytes in attachment_bytes_results:
                attachment_pb = service_pb2.FileAttachment()
                attachment_pb.ParseFromString(attachment_bytes)
                attachment_pb.ClearField("file")
                yield attachment_pb

        except Exception as e:
            logging.error(e)
            await context.abort(grpc.StatusCode.ABORTED, "An internal error occurred")
