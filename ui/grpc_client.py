import os

import grpc

from server import service_pb2_grpc

MPAC_DEBUG = os.getenv("MPAC_DEBUG", "false").lower() == "true"
# Use a plaintext (insecure) gRPC channel to the backend, independent of
# MPAC_DEBUG. On-prem deployments terminate TLS at a front proxy (Envoy) and the
# UI reaches the server plaintext over a private network; this flag lets those
# deployments keep full HTTPS/secure-cookie behavior without forcing debug mode.
MPAC_GRPC_INSECURE = os.getenv("MPAC_GRPC_INSECURE", "false").lower() == "true"
MPAC_HOST = os.getenv("MPAC_HOST", "0.0.0.0")
MPAC_PORT = os.getenv("MPAC_PORT", "50051")
MPAC_JWT_SECRET = os.getenv(
    "MPAC_JWT_SECRET",
    "dev-secret" if os.getenv("MPAC_DEBUG", "false").lower() == "true" else None,
)
MPAC_SYSTEM_USER = os.getenv("MPAC_SYSTEM_USER", "system@mpac.internal")

_MAX_MESSAGE_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB
_GRPC_OPTIONS = [
    ("grpc.max_send_message_length", _MAX_MESSAGE_SIZE),
    ("grpc.max_receive_message_length", _MAX_MESSAGE_SIZE),
]

if MPAC_DEBUG or MPAC_GRPC_INSECURE:
    _channel = grpc.insecure_channel(f"{MPAC_HOST}:{MPAC_PORT}", options=_GRPC_OPTIONS)
else:
    _channel = grpc.secure_channel(
        f"{MPAC_HOST}:{MPAC_PORT}",
        grpc.ssl_channel_credentials(),
        options=_GRPC_OPTIONS,
    )

_stub = service_pb2_grpc.MPACStub(_channel)


def get_mpac_stub() -> service_pb2_grpc.MPACStub:
    return _stub


def get_grpc_metadata(jwt_token: str | None) -> list[tuple[str, str]]:
    """Return gRPC metadata for an authenticated user request."""
    if jwt_token:
        return [("x-jwt-token", jwt_token)]
    return []


def get_system_grpc_metadata() -> list[tuple[str, str]]:
    """Return gRPC metadata for system-level calls (e.g. stale-run reaper).

    Mints a short-lived (5-minute) JWT signed with MPAC_JWT_SECRET so the
    reaper can authenticate to the gRPC server without a standing service key.
    """
    import jwt as pyjwt
    import time

    if not MPAC_JWT_SECRET:
        return []

    payload = {
        "sub": MPAC_SYSTEM_USER,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "aud": "mpac-system",
    }
    token = pyjwt.encode(payload, MPAC_JWT_SECRET, algorithm="HS256")
    return [("x-jwt-token", token)]
