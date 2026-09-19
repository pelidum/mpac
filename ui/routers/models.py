import html
import os
from difflib import SequenceMatcher

import markdown

from absl import logging
from fastapi import APIRouter, Depends, Request
from google.protobuf.json_format import MessageToDict

from server import service_pb2
from ui.dependencies import (
    UserContext,
    get_current_user,
    get_jwt_token,
)
from ui.grpc_client import get_grpc_metadata, get_mpac_stub

router = APIRouter()


def get_provider_icon(provider_name: str | None, static_dir: str) -> str | None:
    if not provider_name:
        return None
    if not os.path.exists(static_dir):
        return None

    available_icons = [f for f in os.listdir(static_dir) if f.endswith(".png")]
    icon_names = [os.path.splitext(f)[0].lower() for f in available_icons]
    provider_lower = provider_name.lower().strip()

    if provider_lower + ".png" in available_icons:
        return provider_lower + ".png"
    for i, icon_name in enumerate(icon_names):
        if provider_lower.startswith(icon_name) or icon_name.startswith(provider_lower):
            return available_icons[i]
    for i, icon_name in enumerate(icon_names):
        if icon_name in provider_lower.split() or provider_lower in icon_name.split():
            return available_icons[i]

    best_match = None
    best_ratio = 0.0
    for i, icon_name in enumerate(icon_names):
        ratio = SequenceMatcher(None, provider_lower, icon_name).ratio()
        len_diff = abs(len(provider_lower) - len(icon_name))
        max_len = max(len(provider_lower), len(icon_name))
        len_diff_ratio = len_diff / max_len if max_len > 0 else 0
        adaptive_threshold = 0.65 + (len_diff_ratio * 0.2)
        if ratio > best_ratio and ratio >= adaptive_threshold:
            best_ratio = ratio
            best_match = available_icons[i]
    return best_match


def _normalize_modality(modality_str) -> str:
    if not modality_str:
        return "TEXT"
    modality_upper = str(modality_str).upper()
    if "IMAGE" in modality_upper or "VISION" in modality_upper:
        return "VISION"
    elif "AUDIO" in modality_upper or "SPEECH" in modality_upper:
        return "AUDIO"
    return "TEXT"


@router.get("/models")
async def models(
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates, STATIC_DIR

    stub = get_mpac_stub()
    metadata = get_grpc_metadata(jwt_token)
    loop = __import__("asyncio").get_running_loop()
    icon_dir = os.path.join(STATIC_DIR, "images", "providers")

    try:
        backends_list = await loop.run_in_executor(
            None,
            lambda: [
                x
                for x in stub.ListBackends(
                    service_pb2.ListRequest(limit=100), metadata=metadata
                )
                if x.enabled
            ],
        )
        backends_by_id = {
            b.id: {
                "id": b.id,
                "name": b.name or b.id,
                "type": service_pb2.BackendType.Name(b.backend_type).lower(),
            }
            for b in backends_list
        }
    except Exception:
        backends_list = []
        backends_by_id = {}

    model_dicts = await loop.run_in_executor(
        None,
        lambda: [
            MessageToDict(
                x,
                always_print_fields_with_no_presence=True,
                preserving_proto_field_name=True,
            )
            for x in stub.ListModels(service_pb2.ListRequest(), metadata=metadata)
        ],
    )

    # Cost tiers
    costs = [
        (
            m.get("pricing", {}).get("input_token_cost", 0)
            + m.get("pricing", {}).get("output_token_cost", 0)
        )
        / 2
        for m in model_dicts
    ]
    non_zero = sorted(c for c in costs if c > 0)
    if non_zero:
        quintiles = [non_zero[int(len(non_zero) * i / 5)] for i in range(1, 5)]
        for i, model in enumerate(model_dicts):
            c = costs[i]
            if c == 0:
                tier = 0
            elif c <= quintiles[0]:
                tier = 1
            elif c <= quintiles[1]:
                tier = 2
            elif c <= quintiles[2]:
                tier = 3
            elif c <= quintiles[3]:
                tier = 4
            else:
                tier = 5
            model["cost_tier"] = tier
    else:
        for model in model_dicts:
            model["cost_tier"] = 0

    for model in model_dicts:
        caps = model.setdefault("capabilities", {})
        modality = caps.get("modality")
        if isinstance(modality, int):
            caps["modality"] = {0: "TEXT", 1: "VISION", 2: "AUDIO"}.get(
                modality, "TEXT"
            )
        else:
            caps["modality"] = _normalize_modality(modality)

    group_map: dict[str, int] = {}
    provider_groups: list[dict] = []
    for model in model_dicts:
        bid = model.get("backend_id", "")
        prov = model.get("provider", "Unknown") or "Unknown"
        binfo = backends_by_id.get(bid, {}) if bid else {}
        key = f"{bid}::{prov}"
        if key not in group_map:
            group_map[key] = len(provider_groups)
            provider_groups.append(
                {
                    "backend_id": bid,
                    "backend_name": binfo.get("name", ""),
                    "backend_type": binfo.get("type", ""),
                    "provider": prov,
                    "icon": get_provider_icon(prov, icon_dir),
                    "models": [],
                }
            )
        provider_groups[group_map[key]]["models"].append(model)

    provider_groups.sort(
        key=lambda g: (g["backend_name"].lower(), g["provider"].lower())
    )

    return templates.TemplateResponse(
        request,
        "models.html",
        {
            "provider_groups": provider_groups,
            "model_dicts": model_dicts,
            "backends": backends_list,
            "backends_by_id": backends_by_id,
            "is_admin": user.is_admin,
        },
    )


@router.get("/models/{model_name:path}")
async def model_detail(
    model_name: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
    jwt_token: str | None = Depends(get_jwt_token),
):
    from ui.server import templates, STATIC_DIR

    stub = get_mpac_stub()
    metadata = get_grpc_metadata(jwt_token)
    loop = __import__("asyncio").get_running_loop()
    icon_dir = os.path.join(STATIC_DIR, "images", "providers")

    model_pb = await loop.run_in_executor(
        None,
        lambda: stub.GetModel(service_pb2.GetRequest(id=model_name), metadata=metadata),
    )
    model_dict = MessageToDict(
        model_pb,
        always_print_fields_with_no_presence=True,
        preserving_proto_field_name=True,
    )
    raw_desc = model_dict.get("description", "")
    model_description = markdown.markdown(html.escape(raw_desc))
    provider = model_dict.get("provider", "Unknown")

    return templates.TemplateResponse(
        request,
        "model_detail.html",
        {
            "model": model_dict,
            "model_description": model_description,
            "provider_icon": get_provider_icon(provider, icon_dir),
            "is_admin": user.is_admin,
        },
    )
