import traceback

from absl import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from server import service_pb2
from ui.dependencies import (
    UserContext,
    get_current_user,
    get_jwt_token,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub

router = APIRouter()


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
        if not attachment_pb.file:
            return Response(status_code=404)
        return Response(
            content=attachment_pb.file,
            media_type=attachment_pb.mime,
            headers={"Cache-Control": "private, max-age=3600"},
        )
    except Exception as e:
        logging.error(e)
        return Response(status_code=404)
