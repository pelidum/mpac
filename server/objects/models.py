import asyncio
import grpc
import ipaddress
import re
import requests
import socket
import traceback

from absl import logging
from google.protobuf.json_format import ParseDict
from google.protobuf.empty_pb2 import Empty

from server import service_pb2


def _validate_backend_url(url: str) -> None:
    """Block requests to private/link-local IP ranges."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
            for _, _, _, _, sockaddr in resolved:
                addr = ipaddress.ip_address(sockaddr[0])
                if addr.is_private or addr.is_loopback or addr.is_link_local:
                    raise ValueError(f"Backend URL resolves to private address: {addr}")
        except socket.gaierror:
            pass
        return
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        raise ValueError(f"Backend URL points to private address: {addr}")


_PARAM_RE = re.compile(
    r"(\d+(?:\.\d+)?[BbMmKk](?:[A-Z]\d+[BbMmKk])?)", re.IGNORECASE
)  # Matches parameter counts
_HF_META_CACHE: dict[str, dict] = {}
_HF_CONFIG_CACHE: dict[str, dict] = {}


def _hf_get(url: str, timeout: int = 8) -> dict:
    """Synchronous HF HTTP fetch — intended for use with asyncio.to_thread."""
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "MPAC/1.0"})
        if r.status_code == 200:
            return r.json()
        logging.debug(f"HF fetch {url} returned HTTP {r.status_code}")
    except Exception as e:
        logging.warning(f"HF fetch failed {url}: {e}")
    return {}


async def _hf_model_info(repo_id: str) -> tuple[dict, dict]:
    """Return (hf_metadata, config_json) for a HuggingFace repo_id.

    Fetches the GGUF repo's API metadata and then the base model's config.json
    (falling back to the GGUF repo's own config.json) in order to recover
    max_position_embeddings. Both results are cached for the process lifetime.
    """
    if repo_id not in _HF_META_CACHE:
        _HF_META_CACHE[repo_id] = await asyncio.to_thread(
            _hf_get, f"https://huggingface.co/api/models/{repo_id}"
        )
    meta = _HF_META_CACHE[repo_id]

    # Prefer the base model's config.json for architecture details.
    card = meta.get("cardData") or {}
    base_model = card.get("base_model")
    if isinstance(base_model, list):
        base_model = base_model[0] if base_model else None
    config_repo = base_model or repo_id

    if config_repo not in _HF_CONFIG_CACHE:
        _HF_CONFIG_CACHE[config_repo] = await asyncio.to_thread(
            _hf_get, f"https://huggingface.co/{config_repo}/resolve/main/config.json"
        )
        if not _HF_CONFIG_CACHE[config_repo] and config_repo != repo_id:
            if repo_id not in _HF_CONFIG_CACHE:
                _HF_CONFIG_CACHE[repo_id] = await asyncio.to_thread(
                    _hf_get,
                    f"https://huggingface.co/{repo_id}/resolve/main/config.json",
                )
            _HF_CONFIG_CACHE[config_repo] = _HF_CONFIG_CACHE[repo_id]

    return meta, _HF_CONFIG_CACHE[config_repo]


def _enrich_llama_cpp_model(raw: dict) -> dict:
    """Derive name/family/quantization/parameters from a llama_swap model ID.

    ID format examples:
      unsloth/GLM-4.7-Flash-GGUF:Q4_K_M
      unsloth/Qwen3.5-122B-A10B-GGUF
      unsloth/gemma-4-26B-A4B-it-GGUF
    """
    model_id = raw.get("id", "")

    # Split owner/repo:quant
    quant = ""
    if ":" in model_id:
        repo_part, quant = model_id.rsplit(":", 1)
    else:
        repo_part = model_id

    provider = repo_part.split("/")[0] if "/" in repo_part else ""
    repo_name = repo_part.split("/")[-1]  # e.g. "GLM-4.7-Flash-GGUF"

    # Strip trailing -GGUF / .GGUF suffix
    clean_name = re.sub(r"[-.]?gguf$", "", repo_name, flags=re.IGNORECASE)

    # Extract parameters label (first match like 7B, 122B, 0.5B)
    param_match = _PARAM_RE.search(clean_name)
    parameters_label = param_match.group(0).upper() if param_match else ""

    # Family = everything before the first parameter token
    if param_match:
        family = clean_name[: param_match.start()].strip("-").strip()
        family = re.sub(r"-[A-Za-z]$", "", family)
    else:
        family = clean_name

    return {
        "id": model_id,
        "name": clean_name,
        "provider": provider,
        "family": family,
        "format": "gguf",
        "quantization": quant,
        "parameters_label": parameters_label,
    }


class ModelsMixin:
    async def _get_enabled_backends(self) -> list[service_pb2.Backend]:
        """Returns enabled backends for inference. Override in tests."""
        if not getattr(self, "db_pool", None):
            return []
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT proto_bytes FROM backends
                       WHERE proto_jsonb ->> 'enabled' = 'true'
                       ORDER BY created_at DESC LIMIT 1000"""
                )
                backends = []
                for row in rows:
                    b = service_pb2.Backend()
                    b.ParseFromString(row["proto_bytes"])
                    backends.append(b)
                return backends
        except Exception as e:
            logging.error(f"Failed to get enabled backends: {e}")
            return []

    async def GetCredits(
        self,
        request: service_pb2.GetRequest,
        context: grpc.aio.ServicerContext,
    ) -> service_pb2.GetCreditsReply:
        get_credits_reply_pb = service_pb2.GetCreditsReply()
        try:
            proto_bytes = await self._grpc_get(id=request.id, obj_type="backends")
            if not proto_bytes:
                await context.abort(
                    grpc.StatusCode.NOT_FOUND,
                    f"Backend {request.id} not found",
                )
            backend_pb = service_pb2.Backend()
            backend_pb.ParseFromString(proto_bytes)
            backend_type = service_pb2.BackendType.Name(backend_pb.backend_type).lower()

            get_credits_reply_pb.backend_type = backend_type
            if backend_type == "openrouter":
                openrouter_credits_endpoint = "https://openrouter.ai/api/v1/credits"
                openrouter_headers = {"Authorization": f"Bearer {backend_pb.api_key}"}
                openrouter_credits_response = requests.get(
                    openrouter_credits_endpoint,
                    headers=openrouter_headers,
                    timeout=3,
                )
                openrouter_credits_response_dict = openrouter_credits_response.json()
                get_credits_reply_pb.total_credits = (
                    openrouter_credits_response_dict.get("data", {}).get(
                        "total_credits", 0
                    )
                )
                get_credits_reply_pb.credits_used = (
                    openrouter_credits_response_dict.get("data", {}).get(
                        "total_usage", 0
                    )
                )
                get_credits_reply_pb.credits_remaining = (
                    get_credits_reply_pb.total_credits
                    - get_credits_reply_pb.credits_used
                )

            return get_credits_reply_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )

    async def GetModel(
        self,
        request: service_pb2.ListRequest,
        context: grpc.aio.ServicerContext,
    ):
        matching_model_pb = service_pb2.Model()
        try:
            async for model_pb in self.ListModels(
                context=context,
                request=service_pb2.ListRequest(),
            ):
                if model_pb.id == request.id:
                    matching_model_pb.CopyFrom(model_pb)
                    return matching_model_pb

        except Exception as e:
            logging.error(e)
            await context.abort(
                grpc.StatusCode.ABORTED,
                "An internal error occurred",
            )
            return
        await context.abort(
            grpc.StatusCode.NOT_FOUND, f"Model {request.id!r} not found"
        )

    async def ListModels(
        self,
        request: service_pb2.ListRequest,
        context: grpc.aio.ServicerContext,
    ):
        # Only yield models from enabled backends
        for backend_pb in await self._get_enabled_backends():
            async for model_pb in self._list_models_for_backend(backend_pb, context):
                yield model_pb

    async def _list_models_for_backend(
        self,
        backend: service_pb2.Backend,
        context: grpc.aio.ServicerContext,
    ):
        backend_type = service_pb2.BackendType.Name(backend.backend_type).lower()
        if backend.base_url and backend_type != "debug_random":
            _validate_backend_url(backend.base_url)
        try:
            match backend_type:
                case "debug_random":
                    know_it_all = {
                        "id": "debug/know_it_all",
                        "name": "Debug Random Model: Know It All 1 Million Billion Parameters",
                        "description": "Your source of PERFECT ALWAYS CORRECT answers that SHOULD NEVER BE USED FOR ANYTHING IMPORTANT.",
                    }
                    for model_dict in [know_it_all] + [
                        {
                            "id": f"debug/debug_random_model_{i + 1}",
                            "name": f"Debug Random Model #{i + 1} 0B",
                            "description": f"Your #{i + 1} source of random answers that SHOULD NEVER BE USED FOR ANYTHING IMPORTANT.",
                        }
                        for i in range(100)
                    ]:
                        m = self._model_dict_to_pb(model_dict, backend_type)
                        m.backend_id = backend.id
                        yield m

                case "ollama":
                    ollama_tags_url = backend.base_url.replace("/v1", "/api/tags")
                    model_details = requests.get(ollama_tags_url, timeout=5)
                    ollama_models = model_details.json().get("models", [])
                    for m in ollama_models:
                        model_dict = {
                            "provider": m.get("model", "").split("/")[0],
                            "id": m.get("model"),
                            "name": m.get("name"),
                            "family": m.get("details", {}).get("family"),
                            "format": m.get("details", {}).get("format"),
                            "parameters_label": m.get("details", {}).get(
                                "parameter_size"
                            ),
                            "quantization": m.get("details", {}).get(
                                "quantization_level"
                            ),
                        }
                        model_pb = service_pb2.Model()
                        ParseDict(js_dict=model_dict, message=model_pb)
                        model_pb.backend_id = backend.id
                        yield model_pb

                case "llama_cpp":
                    headers = {}
                    if backend.api_key:
                        headers["Authorization"] = f"Bearer {backend.api_key}"
                    response = await asyncio.to_thread(
                        requests.get,
                        f"{backend.base_url}/models",
                        headers=headers,
                        timeout=5,
                    )
                    response.raise_for_status()
                    raw_models = response.json().get("data", [])

                    repo_ids = []
                    for raw in raw_models:
                        mid = raw.get("id", "")
                        rid = mid.split(":")[0] if ":" in mid else mid
                        repo_ids.append(rid if "/" in rid else None)

                    async def _empty_hf():
                        return ({}, {})

                    hf_results = await asyncio.gather(
                        *[
                            _hf_model_info(rid) if rid else _empty_hf()
                            for rid in repo_ids
                        ],
                        return_exceptions=True,
                    )

                    for raw, hf_result in zip(raw_models, hf_results):
                        enriched = _enrich_llama_cpp_model(raw)
                        if isinstance(hf_result, tuple):
                            meta, config = hf_result
                            pipeline_tag = meta.get("pipeline_tag", "")
                            if pipeline_tag in (
                                "image-text-to-text",
                                "visual-question-answering",
                                "image-to-text",
                            ):
                                enriched["architecture"] = {
                                    "modality": "text+image-to-text"
                                }
                            elif pipeline_tag == "text-to-speech":
                                enriched["architecture"] = {"modality": "text-to-audio"}
                            cfg = config or {}
                            ctx = cfg.get("max_position_embeddings") or (
                                cfg.get("text_config") or {}
                            ).get("max_position_embeddings")
                            if ctx:
                                enriched["context_length"] = int(ctx)
                            card = meta.get("cardData") or {}
                            lic = card.get("license")
                            if lic and isinstance(lic, str):
                                enriched["license"] = lic
                        m = self._model_dict_to_pb(enriched, backend_type)
                        m.backend_id = backend.id
                        yield m

                case _:
                    headers = {}
                    if backend.api_key:
                        headers["Authorization"] = f"Bearer {backend.api_key}"
                    response = await asyncio.to_thread(
                        requests.get,
                        f"{backend.base_url}/models",
                        headers=headers,
                        timeout=5,
                    )
                    response.raise_for_status()
                    for model_dict in response.json().get("data", []):
                        m = self._model_dict_to_pb(model_dict, backend_type)
                        m.backend_id = backend.id
                        yield m

        except Exception as e:
            logging.error(
                f"Failed to list models for backend {backend.id} ({backend_type}): {e}"
            )
            logging.error(traceback.format_exc())

    def _model_dict_to_pb(
        self, model_dict: dict, backend_type: str
    ) -> service_pb2.Model:
        model_pb = service_pb2.Model()

        model_provider = model_dict.get("id", "").split("/")
        if model_provider and len(model_provider) > 1:
            model_pb.provider = model_provider[0]

        model_id = model_dict.get("id")
        if model_id is not None:
            model_pb.id = model_id

        model_name = model_dict.get("name")
        if model_name is not None:
            model_pb.name = model_name

        model_description = model_dict.get("description")
        if model_description is not None:
            model_pb.description = model_description

        model_pricing = model_dict.get("pricing", {})
        pricing_info_dict = {"prompt": 0, "completion": 0, "image": 0, "audio": 0}
        try:
            for pricing_key in pricing_info_dict.keys():
                cost_data = float(model_pricing.get(pricing_key, 0))
                pricing_info_dict[pricing_key] = cost_data
        except Exception as e:
            logging.error(traceback.format_exc())
            logging.error(e)

        model_pb.pricing.input_token_cost = pricing_info_dict["prompt"]
        model_pb.pricing.output_token_cost = pricing_info_dict["completion"]
        model_pb.pricing.image_token_cost = pricing_info_dict["image"]
        model_pb.pricing.audio_token_cost = pricing_info_dict["audio"]

        context_length = model_dict.get("context_length")
        if context_length is not None:
            model_pb.capabilities.context_window_length = context_length
        elif backend_type != "llama_cpp":
            model_pb.capabilities.context_window_length = 8192

        architecture_dict = model_dict.get("architecture", {})
        arch_modality = (
            architecture_dict.get("modality")
            if isinstance(architecture_dict, dict)
            else None
        )

        if arch_modality is not None:
            model_pb.capabilities.modality = arch_modality
        elif backend_type == "llama_cpp":
            name_tokens = (
                model_dict.get("name", "") + " " + model_dict.get("id", "")
            ).lower()
            if any(
                k in name_tokens
                for k in [
                    "vision",
                    "-vl",
                    "vl-",
                    "llava",
                    "minicpm-v",
                    "pixtral",
                    "qvq",
                    "internvl",
                ]
            ):
                model_pb.capabilities.modality = "text+image-to-text"
            else:
                model_pb.capabilities.modality = "text-to-text"
        else:
            model_pb.capabilities.modality = "text-to-text"

        if isinstance(architecture_dict, dict):
            if architecture_dict.get("tokenizer") is not None:
                model_pb.capabilities.tokenizer = architecture_dict.get("tokenizer")

            if architecture_dict.get("instruct_type") is not None:
                model_pb.capabilities.instruct = architecture_dict.get("instruct_type")

        model_license = model_dict.get("license")
        if model_license is not None:
            model_pb.license = model_license

        return model_pb
