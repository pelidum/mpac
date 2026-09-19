import hashlib
import secrets
import traceback

from absl import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict

from server import service_pb2
from ui.dependencies import (
    UserContext,
    get_current_user,
    get_jwt_token,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub

router = APIRouter()


@router.get("/settings")
async def settings(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    stub = get_mpac_stub()
    metadata = get_grpc_metadata(jwt_token)
    loop = __import__("asyncio").get_running_loop()

    user_pb = await loop.run_in_executor(
        None,
        lambda: stub.GetUser(service_pb2.GetRequest(id=user.email), metadata=metadata),
    )
    user_dict = MessageToDict(
        user_pb,
        always_print_fields_with_no_presence=True,
        preserving_proto_field_name=True,
    )

    test_count = await loop.run_in_executor(
        None,
        lambda: sum(
            1
            for t in stub.ListTests(
                service_pb2.ListRequest(limit=500), metadata=metadata
            )
            if t.owner == user.email
        ),
    )
    run_count = await loop.run_in_executor(
        None,
        lambda: sum(
            1
            for r in stub.ListTestRuns(
                service_pb2.ListRequest(limit=500), metadata=metadata
            )
            if r.owner == user.email
        ),
    )

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "page_title": "Settings",
            "user": user_dict,
            "test_count": test_count,
            "run_count": run_count,
            "daily_spend_usd": user_pb.spend.daily_usd,
            "monthly_spend_usd": user_pb.spend.monthly_usd,
            "is_admin": user.is_admin,
        },
    )


@router.post("/settings/mint_api_key")
async def mint_api_key(
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        user_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetUser(
                service_pb2.GetRequest(id=user.email), metadata=metadata
            ),
        )
        raw_key = f"sk_live_{secrets.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        truncated = f"{raw_key[:12]}{'.' * 24}{raw_key[-4:]}"

        user_pb.api_key.name = f"{user.name}'s MPAC API Key"
        user_pb.api_key.hash = key_hash
        user_pb.api_key.active = True
        user_pb.api_key.created_at_utc.GetCurrentTime()
        user_pb.api_key.display_name = truncated

        await loop.run_in_executor(
            None,
            lambda: stub.UpdateUser(user_pb, metadata=metadata),
        )
        return JSONResponse({"success": True, "raw_key": raw_key})
    except Exception as e:
        logging.error(f"Error minting API key: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred."}, status_code=500
        )
