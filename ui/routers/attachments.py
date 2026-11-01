import os
import traceback

import grpc
import httpx
from absl import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from proto import service_pb2
from ui import blobs
from ui.dependencies import (
    UserContext,
    get_current_user,
    get_jwt_token,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub

router = APIRouter()

_UPLOAD_ERROR_STATUS = {
    grpc.StatusCode.INVALID_ARGUMENT: 400,
    grpc.StatusCode.UNAUTHENTICATED: 401,
    grpc.StatusCode.PERMISSION_DENIED: 403,
    grpc.StatusCode.ALREADY_EXISTS: 409,
}
_INLINE_ONLY_CODES = {
    grpc.StatusCode.UNIMPLEMENTED,
    grpc.StatusCode.FAILED_PRECONDITION,
}


@router.post("/attachments/upload")
async def attachment_upload(
    request: Request,
    name: str,
    mime: str = "",
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        size = int(request.headers.get("content-length", ""))
    except ValueError:
        size = 0
    if size <= 0:
        return JSONResponse({"error": "A file size is required"}, status_code=411)
    limit = blobs.max_attachment_bytes()
    if size > limit:
        return JSONResponse(
            {"error": f"File size {size} bytes exceeds maximum of {limit} bytes"},
            status_code=413,
        )
    try:
        attachment_id = await blobs.upload_stream(
            get_mpac_stub(),
            get_grpc_metadata(jwt_token),
            name,
            size,
            request.stream(),
            mime,
        )
    except grpc.RpcError as error:
        if error.code() in _INLINE_ONLY_CODES:
            return JSONResponse({"inline": True}, status_code=501)
        status = _UPLOAD_ERROR_STATUS.get(error.code())
        if status is None:
            logging.error(error)
            return JSONResponse({"error": "Upload failed"}, status_code=500)
        return JSONResponse({"error": error.details()}, status_code=status)
    except ClientDisconnect:
        return Response(status_code=499)
    except httpx.HTTPError as error:
        logging.error(f"Attachment transfer failed: {error}")
        return JSONResponse({"error": "Upload to storage failed"}, status_code=502)
    return JSONResponse({"id": attachment_id})


@router.get("/attachments/{attachment_id}")
async def attachment_details(
    attachment_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        attachment_pb = await loop.run_in_executor(
            None,
            lambda: next(
                iter(
                    stub.BatchGetAttachments(
                        service_pb2.BatchGetAttachmentsRequest(
                            ids=[attachment_id], metadata_only=True
                        ),
                        metadata=metadata,
                    )
                ),
                None,
            ),
        )
        if not attachment_pb or not attachment_pb.id:
            return templates.TemplateResponse(
                request, "404.html", {"is_admin": user.is_admin}, status_code=404
            )
        return templates.TemplateResponse(
            request,
            "attachment_details.html",
            {"attachment_pb": attachment_pb, "is_admin": user.is_admin},
        )
    except Exception:
        logging.error(traceback.format_exc())
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": user.is_admin}, status_code=404
        )


@router.get("/attachments/{attachment_id}/file")
async def attachment_file(
    attachment_id: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        attachment_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetAttachment(
                service_pb2.GetRequest(id=attachment_id), metadata=metadata
            ),
        )
        if attachment_pb.uri and not os.environ.get("MPAC_BLOB_INTERNAL_ENDPOINT"):
            return RedirectResponse(attachment_pb.uri, status_code=302)
        if not attachment_pb.uri:
            if not attachment_pb.file:
                return Response(status_code=404)
            return Response(
                content=attachment_pb.file,
                media_type=attachment_pb.mime,
                headers={"Cache-Control": "private, max-age=3600"},
            )
        # On-prem signed URLs may name a different host from the browser's UI
        # origin. Fetch through the internal transport, preserving the signed
        # Host header, and serve media with its recorded MIME type rather than
        # Seaweed's upload-default Content-Type.
        upstream = await loop.run_in_executor(
            None, blobs.open_content, attachment_pb, request.headers.get("range")
        )
        headers = {"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes"}
        for name in ("Content-Length", "Content-Range"):
            if name in upstream.headers:
                headers[name] = upstream.headers[name]
        return StreamingResponse(
            upstream.iter_content(blobs.TRANSFER_CHUNK_BYTES),
            status_code=upstream.status_code,
            media_type=attachment_pb.mime,
            headers=headers,
            background=BackgroundTask(upstream.close),
        )
    except Exception as e:
        logging.error(e)
        return Response(status_code=404)
