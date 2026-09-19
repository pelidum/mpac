import traceback

from absl import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict, ParseDict
from werkzeug.security import generate_password_hash

from server import service_pb2
from ui.dependencies import (
    UserContext,
    get_jwt_token,
    require_admin,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub

router = APIRouter()

_UNAUTH = JSONResponse({"success": False, "error": "Not authorized."}, status_code=401)


@router.get("/admin")
async def admin(
    request: Request,
    user: UserContext = Depends(require_admin),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates

    stub = get_mpac_stub()
    metadata = get_grpc_metadata(jwt_token)
    loop = __import__("asyncio").get_running_loop()

    users = await loop.run_in_executor(
        None,
        lambda: [
            MessageToDict(
                u,
                preserving_proto_field_name=True,
                always_print_fields_with_no_presence=True,
            )
            for u in stub.ListUsers(
                service_pb2.ListRequest(limit=50), metadata=metadata
            )
        ],
    )
    backends = await loop.run_in_executor(
        None,
        lambda: [
            MessageToDict(
                b,
                preserving_proto_field_name=True,
                always_print_fields_with_no_presence=True,
            )
            for b in stub.ListBackends(
                service_pb2.ListRequest(limit=100), metadata=metadata
            )
        ],
    )

    return templates.TemplateResponse(
        request,
        "admin.html",
        {"page_title": "Admin", "users": users, "backends": backends, "is_admin": True},
    )


@router.get("/admin/backend_credits/{backend_id}")
async def admin_backend_credits(
    backend_id: str,
    user: UserContext = Depends(require_admin),
    jwt_token: str | None = Depends(get_jwt_token),
):
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        credits_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetCredits(
                service_pb2.GetRequest(id=backend_id), metadata=metadata
            ),
        )
        return JSONResponse(
            MessageToDict(
                credits_pb,
                always_print_fields_with_no_presence=True,
                preserving_proto_field_name=True,
            )
        )
    except Exception as e:
        logging.error(f"Error fetching credits for {backend_id}: {e}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred"}, status_code=500
        )


@router.post("/admin/add_backend")
async def admin_add_backend(
    request: Request,
    user: UserContext = Depends(require_admin),
    jwt_token: str | None = Depends(get_jwt_token),
):
    body = await request.body()
    if not body:
        return JSONResponse(
            {"success": False, "error": "No data provided."}, status_code=400
        )
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        backend_dict = await request.json()
        backend_pb = service_pb2.Backend()
        ParseDict(js_dict=backend_dict, message=backend_pb, ignore_unknown_fields=True)
        response = await loop.run_in_executor(
            None, lambda: stub.CreateBackend(backend_pb, metadata=metadata)
        )
        return JSONResponse({"success": response.code == 1, "id": response.id})
    except Exception as e:
        logging.error(f"Error adding backend: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred."}, status_code=500
        )


@router.post("/admin/update_backend")
async def admin_update_backend(
    request: Request,
    user: UserContext = Depends(require_admin),
    jwt_token: str | None = Depends(get_jwt_token),
):
    body = await request.body()
    if not body:
        return JSONResponse(
            {"success": False, "error": "No data provided."}, status_code=400
        )
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        backend_dict = await request.json()
        backend_id = backend_dict.get("id")
        if not backend_id:
            return JSONResponse(
                {"success": False, "error": "Backend ID is required."}, status_code=400
            )
        backend_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetBackend(
                service_pb2.GetRequest(id=backend_id), metadata=metadata
            ),
        )
        ParseDict(js_dict=backend_dict, message=backend_pb, ignore_unknown_fields=True)
        response = await loop.run_in_executor(
            None, lambda: stub.UpdateBackend(backend_pb, metadata=metadata)
        )
        return JSONResponse({"success": response.code == 1})
    except Exception as e:
        logging.error(f"Error updating backend: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred."}, status_code=500
        )


@router.post("/admin/toggle_backend")
async def admin_toggle_backend(
    request: Request,
    user: UserContext = Depends(require_admin),
    jwt_token: str | None = Depends(get_jwt_token),
):
    data = await request.json() or {}
    backend_id = data.get("id")
    enabled = data.get("enabled")
    if not backend_id or enabled is None:
        return JSONResponse(
            {"success": False, "error": "Missing id or enabled."}, status_code=400
        )
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        backend_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetBackend(
                service_pb2.GetRequest(id=backend_id), metadata=metadata
            ),
        )
        backend_pb.enabled = bool(enabled)
        response = await loop.run_in_executor(
            None, lambda: stub.UpdateBackend(backend_pb, metadata=metadata)
        )
        return JSONResponse({"success": response.code == 1})
    except Exception as e:
        logging.error(
            f"Error toggling backend {backend_id}: {e}\n{traceback.format_exc()}"
        )
        return JSONResponse(
            {"success": False, "error": "An internal error occurred."}, status_code=500
        )


@router.post("/admin/delete_backend")
async def admin_delete_backend(
    request: Request,
    user: UserContext = Depends(require_admin),
    jwt_token: str | None = Depends(get_jwt_token),
):
    body = await request.body()
    if not body:
        return JSONResponse(
            {"success": False, "error": "No data provided."}, status_code=400
        )
    data = await request.json()
    backend_id = data.get("id")
    if not backend_id:
        return JSONResponse(
            {"success": False, "error": "No backend ID provided."}, status_code=400
        )
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        response = await loop.run_in_executor(
            None,
            lambda: stub.DeleteBackend(
                service_pb2.DeleteRequest(id=backend_id), metadata=metadata
            ),
        )
        return JSONResponse({"success": response.code == 1})
    except Exception as e:
        logging.error(f"Error deleting backend: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred."}, status_code=500
        )


@router.post("/admin/add_user")
async def admin_add_user(
    request: Request,
    user: UserContext = Depends(require_admin),
    jwt_token: str | None = Depends(get_jwt_token),
):
    body = await request.body()
    if not body:
        return JSONResponse(
            {"success": False, "error": "Invalid user data provided"}, status_code=400
        )
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        user_dict = await request.json()
        plain_password = user_dict.pop("password", None)
        if plain_password:
            user_dict["password_hash"] = generate_password_hash(plain_password)

        user_pb = service_pb2.User()
        ParseDict(js_dict=user_dict, message=user_pb, ignore_unknown_fields=True)
        response = await loop.run_in_executor(
            None, lambda: stub.CreateUser(user_pb, metadata=metadata)
        )
        return JSONResponse({"success": response.code == 1})
    except Exception as e:
        logging.error(f"Error adding user: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred."}, status_code=500
        )


@router.post("/admin/delete_user")
async def admin_delete_user(
    request: Request,
    user: UserContext = Depends(require_admin),
    jwt_token: str | None = Depends(get_jwt_token),
):
    body = await request.body()
    if not body:
        return JSONResponse(
            {"success": False, "error": "Invalid user data provided"}, status_code=400
        )
    data = await request.json()
    user_id = data.get("id")
    if not user_id:
        return JSONResponse(
            {"success": False, "error": "No user data provided"}, status_code=400
        )
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        response = await loop.run_in_executor(
            None,
            lambda: stub.DeleteUser(
                service_pb2.DeleteRequest(id=user_id), metadata=metadata
            ),
        )
        return JSONResponse({"success": response.code == 1})
    except Exception as e:
        logging.error(f"Error deleting user: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred."}, status_code=500
        )


@router.post("/admin/update_user")
async def admin_update_user(
    request: Request,
    user: UserContext = Depends(require_admin),
    jwt_token: str | None = Depends(get_jwt_token),
):
    body = await request.body()
    if not body:
        return JSONResponse(
            {"success": False, "error": "Invalid user data provided"}, status_code=400
        )
    try:
        stub = get_mpac_stub()
        metadata = get_grpc_metadata(jwt_token)
        loop = __import__("asyncio").get_running_loop()

        user_dict = await request.json()
        plain_password = user_dict.pop("password", None)
        if plain_password:
            user_dict["password_hash"] = generate_password_hash(plain_password)

        user_pb = await loop.run_in_executor(
            None,
            lambda: stub.GetUser(
                service_pb2.GetRequest(id=user_dict.get("id")), metadata=metadata
            ),
        )
        ParseDict(js_dict=user_dict, message=user_pb, ignore_unknown_fields=True)
        response = await loop.run_in_executor(
            None, lambda: stub.UpdateUser(user_pb, metadata=metadata)
        )
        return JSONResponse({"success": response.code == 1})
    except Exception as e:
        logging.error(f"Error updating user: {e}\n{traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "error": "An internal error occurred."}, status_code=500
        )
