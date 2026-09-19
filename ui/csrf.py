import hmac
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse

_EXEMPT_PREFIXES = (
    "/login/",
    "/callback/",
)


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def _get_submitted_token(request: Request) -> str | None:
    token = request.headers.get("x-csrf-token")
    if token:
        return token
    content_type = request.headers.get("content-type", "")
    if (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        form = await request.form()
        return form.get("csrf_token")
    return None


def _is_exempt(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


async def csrf_middleware(request: Request, call_next):
    token = ensure_csrf_token(request)
    request.state.csrf_token = token

    if request.method in ("POST", "PUT", "DELETE", "PATCH") and not _is_exempt(
        request.url.path
    ):
        session_token = request.session.get("csrf_token", "")
        submitted_token = await _get_submitted_token(request) or ""
        if not hmac.compare_digest(session_token, submitted_token):
            return JSONResponse(
                {"detail": "CSRF token missing or invalid."}, status_code=403
            )

    return await call_next(request)
