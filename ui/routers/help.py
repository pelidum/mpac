import json

from absl import logging
from fastapi import APIRouter, Depends, Request

from ui.dependencies import UserContext, get_current_user

router = APIRouter()

_PROTO_JSON_PATH = "server/mpac_proto_json/mpac_proto_json.json"


@router.get("/help")
async def help(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    from ui.server import templates

    mpac_object_info = {}
    mpac_service_methods = {}
    try:
        with open(_PROTO_JSON_PATH) as f:
            mpac_proto_json = json.loads(f.read())
            for mpac_file in mpac_proto_json.get("files", []):
                for obj_type in ["enums", "messages"]:
                    for obj in mpac_file.get(obj_type, []):
                        mpac_object_info[obj.get("name")] = obj
                for svc in mpac_file.get("services", []):
                    for method in svc.get("methods", []):
                        mpac_service_methods[method.get("name")] = method
    except Exception as e:
        logging.error(e)

    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "page_title": "Help",
            "mpac_object_info": mpac_object_info,
            "mpac_service_methods": mpac_service_methods,
            "is_admin": user.is_admin,
        },
    )
