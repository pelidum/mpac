from urllib.parse import quote

import jwt as pyjwt

from fastapi import Depends, HTTPException, Request

from ui.auth import UserContext, decode_jwt


def get_jwt_token(request: Request) -> str | None:
    """Extract the raw JWT string from the session cookie."""
    return request.cookies.get("mpac_jwt")


_QUOTE_SAFE = "/:@!$&'()*+,;=-._~"


def _login_redirect(request: Request) -> str:
    path = request.url.path
    if path and path != "/" and not path.startswith("/login"):
        return f"/login?next={quote(path, safe=_QUOTE_SAFE)}"
    return "/login"


def get_current_user(request: Request) -> UserContext:
    """FastAPI dependency: decode JWT cookie → UserContext, or redirect to /login."""
    login_url = _login_redirect(request)
    token = request.cookies.get("mpac_jwt")
    if not token:
        raise HTTPException(status_code=303, headers={"Location": login_url})
    try:
        return decode_jwt(token)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=303, headers={"Location": login_url})
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=303, headers={"Location": login_url})


def require_admin(
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    """Like get_current_user but additionally asserts is_admin."""
    if not user.is_admin:
        raise HTTPException(status_code=404)
    return user
