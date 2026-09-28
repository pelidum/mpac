import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import grpc
import jwt as pyjwt

from absl import logging
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from starlette.config import Config

from server import service_pb2
from ui.grpc_client import (
    get_mpac_stub,
    MPAC_JWT_SECRET,
    MPAC_DEBUG,
)

_JWT_ALGORITHM = "HS256"
_JWT_EXPIRY_HOURS = 1
_SESSION_MAX_AGE_HOURS = 24
_RECONNECT_EXPIRY_HOURS = 24

_TOKEN_DENYLIST: dict[str, float] = {}
_DENYLIST_MAX_SIZE = 10000

GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")


def google_oauth_enabled() -> bool:
    """Google sign-in is optional; it's offered only when a client is configured."""
    return bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET)


_starlette_config = Config(
    environ={
        "GOOGLE_CLIENT_ID": GOOGLE_OAUTH_CLIENT_ID or "",
        "GOOGLE_CLIENT_SECRET": GOOGLE_OAUTH_CLIENT_SECRET or "",
    }
)

_oauth = OAuth(_starlette_config)
_oauth.register(
    name="google",
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@dataclass
class UserContext:
    email: str
    name: str
    avatar_url: str
    is_admin: bool
    org_domain: str
    login_at: float = 0.0  # unix timestamp of original login, carried through renewals


def create_jwt(
    email: str,
    name: str,
    avatar_url: str,
    is_admin: bool = False,
    org_domain: str = "",
    login_at: float | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email,
        "name": name,
        "avatar_url": avatar_url,
        "is_admin": is_admin,
        "org_domain": org_domain,
        "login_at": login_at if login_at is not None else now.timestamp(),
        "exp": now + timedelta(hours=_JWT_EXPIRY_HOURS),
        "iat": now,
        "jti": str(uuid.uuid4()),
        "aud": "mpac-web",
    }
    return pyjwt.encode(payload, MPAC_JWT_SECRET, algorithm=_JWT_ALGORITHM)


def _prune_denylist() -> None:
    now = time.time()
    expired = [jti for jti, exp in _TOKEN_DENYLIST.items() if exp <= now]
    for jti in expired:
        _TOKEN_DENYLIST.pop(jti, None)


def revoke_token(token: str) -> None:
    try:
        payload = pyjwt.decode(
            token,
            MPAC_JWT_SECRET,
            algorithms=[_JWT_ALGORITHM],
            audience="mpac-web",
        )
        jti = payload.get("jti")
        if jti:
            _prune_denylist()
            if len(_TOKEN_DENYLIST) < _DENYLIST_MAX_SIZE:
                _TOKEN_DENYLIST[jti] = payload.get("exp", time.time() + 3600)
    except pyjwt.PyJWTError:
        pass


def decode_jwt(token: str) -> UserContext:
    payload = pyjwt.decode(
        token,
        MPAC_JWT_SECRET,
        algorithms=[_JWT_ALGORITHM],
        audience="mpac-web",
    )
    jti = payload.get("jti")
    if jti and jti in _TOKEN_DENYLIST:
        raise pyjwt.InvalidTokenError("Token has been revoked")
    return UserContext(
        email=payload["sub"],
        name=payload.get("name", ""),
        avatar_url=payload.get("avatar_url", ""),
        is_admin=payload.get("is_admin", False),
        org_domain=payload.get("org_domain", ""),
        login_at=payload.get("login_at", 0.0),
    )


def create_reconnection_token(run_id: str, user_email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "run_id": run_id,
        "user_email": user_email,
        "purpose": "stream_reconnection",
        "exp": now + timedelta(hours=_RECONNECT_EXPIRY_HOURS),
        "iat": now,
        "aud": "mpac-reconnect",
    }
    return pyjwt.encode(payload, MPAC_JWT_SECRET, algorithm=_JWT_ALGORITHM)


def validate_reconnection_token(token: str) -> dict:
    payload = pyjwt.decode(
        token,
        MPAC_JWT_SECRET,
        algorithms=[_JWT_ALGORITHM],
        audience="mpac-reconnect",
    )
    if payload.get("purpose") != "stream_reconnection":
        raise pyjwt.InvalidTokenError("Invalid token purpose")
    if not payload.get("run_id") or not payload.get("user_email"):
        raise pyjwt.InvalidTokenError("Missing required token fields")
    return payload


def _set_jwt_cookie(
    response: Response, jwt_token: str, max_age: int = _JWT_EXPIRY_HOURS * 3600
) -> None:
    response.set_cookie(
        "mpac_jwt",
        jwt_token,
        httponly=True,
        secure=not MPAC_DEBUG,
        samesite="lax",
        max_age=max_age,
    )


def try_renew_cookie(response: Response, token: str) -> None:
    """Sliding-window renewal: reissue cookie on each request, capped at 24h from login_at."""
    try:
        user = decode_jwt(token)
    except pyjwt.PyJWTError:
        return
    if not user.login_at:
        return  # old token without login_at — let it expire naturally
    remaining = int(user.login_at + _SESSION_MAX_AGE_HOURS * 3600 - time.time())
    if remaining <= 0:
        return  # hard 24h cap reached
    max_age = min(_JWT_EXPIRY_HOURS * 3600, remaining)
    new_token = create_jwt(
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        is_admin=user.is_admin,
        org_domain=user.org_domain,
        login_at=user.login_at,
    )
    _set_jwt_cookie(response, new_token, max_age=max_age)


# ---------------------------------------------------------------------------
# Auth router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/login")
async def login(request: Request):
    from ui.server import templates

    next_url = _safe_next_url(request.query_params.get("next", ""))
    return templates.TemplateResponse(request, "login.html", {"next_url": next_url})


@router.get("/login/password")
async def login_password(request: Request):
    next_url = _safe_next_url(request.query_params.get("next", ""))
    token = request.cookies.get("mpac_jwt")
    if token:
        try:
            decode_jwt(token)
            return RedirectResponse(url=next_url or "/", status_code=303)
        except pyjwt.PyJWTError:
            pass
    from ui.server import templates

    return templates.TemplateResponse(
        request, "login_password.html", {"next_url": next_url}
    )


@router.post("/login/password")
async def login_password_post(request: Request):
    from ui.rate_limit import login_limiter
    from ui.server import templates

    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "")
    next_url = _safe_next_url(form.get("next", ""))

    client_ip = request.client.host if request.client else "unknown"
    if login_limiter.is_blocked(client_ip):
        retry_after = login_limiter.seconds_until_unblocked(client_ip)
        return templates.TemplateResponse(
            request,
            "login_password.html",
            {
                "error": "Too many login attempts. Please try again later.",
                "next_url": next_url,
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    if not username or not password:
        return templates.TemplateResponse(
            request,
            "login_password.html",
            {"error": "Please enter both username and password.", "next_url": next_url},
            status_code=400,
        )

    try:
        stub = get_mpac_stub()
        user_pb = stub.LoginUser(
            service_pb2.LoginRequest(user_id=username, password=password),
        )
        if not user_pb.id:
            login_limiter.record_failure(client_ip)
            return templates.TemplateResponse(
                request,
                "login_password.html",
                {"error": "Invalid username or password.", "next_url": next_url},
                status_code=401,
            )

        login_limiter.clear(client_ip)
        is_admin = user_pb.role == 2
        jwt_token = create_jwt(
            email=user_pb.id,
            name=user_pb.name,
            avatar_url=user_pb.avatar_url,
            is_admin=is_admin,
            org_domain=user_pb.org_domain,
        )
        response = RedirectResponse(url=next_url or "/", status_code=303)
        _set_jwt_cookie(response, jwt_token)
        return response

    except grpc.RpcError as e:
        login_limiter.record_failure(client_ip)
        logging.warning(f"Password login failed for {username}: {e.details()}")
        return templates.TemplateResponse(
            request,
            "login_password.html",
            {"error": "Invalid username or password.", "next_url": next_url},
            status_code=401,
        )


@router.get("/authorize/{provider}")
async def authorize(provider: str, request: Request):
    if provider != "google":
        return Response(f"Unknown OAuth provider: {provider}", status_code=400)
    if not google_oauth_enabled():
        # Without a client, Google would only show an invalid_client error.
        return RedirectResponse(url="/login", status_code=303)

    next_url = request.query_params.get("next", "")
    if next_url:
        request.session["login_next"] = next_url

    scheme = "http" if MPAC_DEBUG else "https"
    redirect_uri = f"{scheme}://{request.headers.get('host', request.base_url.netloc)}/callback/{provider}"
    return await _oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback/{provider}")
async def callback(provider: str, request: Request):
    from ui.server import templates

    if provider != "google":
        return Response("Unknown OAuth provider", status_code=400)

    try:
        token = await _oauth.google.authorize_access_token(request)
        if not token:
            logging.warning("OAuth callback: Failed to get access token")
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Failed to authenticate. Please try again."},
            )

        userinfo_url = "https://www.googleapis.com/oauth2/v3/userinfo"
        resp = await _oauth.google.get(userinfo_url, token=token)
        userinfo = resp.json()
        email = userinfo.get("email")

        # Bootstrap JWT for this GetUser call: use a temporary service-style token
        # signed with the same MPAC_JWT_SECRET so the gRPC interceptor accepts it.
        bootstrap_token = _mint_bootstrap_token(email)
        stub = get_mpac_stub()
        user_pb = stub.GetUser(
            service_pb2.GetRequest(id=email),
            metadata=[("x-jwt-token", bootstrap_token)],
        )

        if not user_pb.id:
            logging.warning(f"OAuth callback: user {email} not found in MPAC")
            return templates.TemplateResponse(
                request, "login.html", {"error": "Access Denied: User not found."}
            )

        is_admin = user_pb.role == 2
        jwt_token = create_jwt(
            email=email,
            name=userinfo.get("name", ""),
            avatar_url=userinfo.get("picture", ""),
            is_admin=is_admin,
            org_domain=userinfo.get("hd", ""),
        )

        # Update user metadata from OAuth token (best-effort)
        try:
            user_pb.name = userinfo.get("name", "")
            user_pb.avatar_url = userinfo.get("picture", "")
            user_pb.verified = userinfo.get("email_verified", False)
            user_pb.org_domain = userinfo.get("hd", "")
            user_pb.last_login.GetCurrentTime()
            stub.UpdateUser(
                user_pb,
                metadata=[("x-jwt-token", bootstrap_token)],
            )
        except Exception as e:
            logging.error(f"Unable to update user info from OAuth token: {e}")

        logging.info(f"OAuth login successful: {email}")
        next_url = _safe_next_url(request.session.pop("login_next", ""))
        response = RedirectResponse(url=next_url or "/", status_code=303)
        _set_jwt_cookie(response, jwt_token)
        return response

    except Exception as e:
        error_msg = str(e)
        logging.error(f"OAuth callback error: {error_msg}")
        if "invalid_grant" in error_msg or "Bad Request" in error_msg:
            msg = "Login session expired. Please try again."
        else:
            msg = "Authentication error. Please try again."
        return templates.TemplateResponse(request, "login.html", {"error": msg})


@router.get("/logout")
async def logout(request: Request):
    from ui.server import templates

    user_email = "unknown"
    token = request.cookies.get("mpac_jwt")
    if token:
        try:
            ctx = decode_jwt(token)
            user_email = ctx.email
        except pyjwt.PyJWTError:
            pass
    logging.info(f"User logout: {user_email}")
    if token:
        revoke_token(token)
    response = templates.TemplateResponse(
        request, "login.html", {"message": "You have been logged out successfully."}
    )
    response.delete_cookie("mpac_jwt")
    return response


def _safe_next_url(url: str) -> str:
    """Only allow relative paths to prevent open-redirect attacks."""
    if not url or not url.startswith("/") or url.startswith("//"):
        return ""
    return url


def _mint_bootstrap_token(email: str) -> str:
    """Short-lived (5-minute) JWT for the OAuth callback, before a full session JWT exists."""
    payload = {
        "sub": email,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "name": "",
        "avatar_url": "",
        "is_admin": False,
        "org_domain": "",
        "aud": "mpac-web",
    }
    return pyjwt.encode(payload, MPAC_JWT_SECRET, algorithm=_JWT_ALGORITHM)
