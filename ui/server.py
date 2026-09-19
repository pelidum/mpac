import os
import re as _re
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from ui import auth
from ui.auth import try_renew_cookie
from ui.csrf import csrf_middleware, ensure_csrf_token
from ui.grpc_client import MPAC_DEBUG, MPAC_JWT_SECRET
from ui.routers import (
    admin,
    attachments,
    benchmarks,
    help,
    home,
    metrics,
    models,
    runs,
    settings,
    tests,
)
from ui.state import start_reaper

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import asyncio

    reaper_task = asyncio.create_task(start_reaper())
    yield
    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass


def create_app() -> FastAPI:
    app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(BaseHTTPMiddleware, dispatch=csrf_middleware)
    app.add_middleware(
        SessionMiddleware, secret_key=MPAC_JWT_SECRET, https_only=not MPAC_DEBUG
    )
    _trusted = os.getenv("MPAC_TRUSTED_PROXIES", "127.0.0.1").split(",")
    app.add_middleware(
        ProxyHeadersMiddleware, trusted_hosts=[h.strip() for h in _trusted]
    )

    @app.middleware("http")
    async def sliding_session(request: Request, call_next):
        response = await call_next(request)
        token = request.cookies.get("mpac_jwt")
        if token:
            try_renew_cookie(response, token)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        return response

    app.include_router(auth.router)
    app.include_router(home.router)
    app.include_router(runs.router)
    app.include_router(tests.router)
    app.include_router(models.router)
    app.include_router(metrics.router)
    app.include_router(benchmarks.router)
    app.include_router(settings.router)
    app.include_router(admin.router)
    app.include_router(attachments.router)
    app.include_router(help.router)

    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR, follow_symlink=True),
        name="static",
    )

    def _url_for(name: str, **kwargs) -> str:
        # Flask used filename=, Starlette StaticFiles uses path=
        if name == "static" and "filename" in kwargs:
            kwargs["path"] = kwargs.pop("filename")
        try:
            return str(app.url_path_for(name, **kwargs))
        except Exception:
            # Flask compat: extra kwargs that aren't path params become query string
            for route in app.routes:
                if getattr(route, "name", None) == name:
                    path_param_names = set(
                        _re.findall(r"\{(\w+)\}", getattr(route, "path", ""))
                    )
                    base = str(
                        app.url_path_for(
                            name,
                            **{
                                k: v for k, v in kwargs.items() if k in path_param_names
                            },
                        )
                    )
                    extra = {
                        k: v for k, v in kwargs.items() if k not in path_param_names
                    }
                    return base + ("?" + urlencode(extra) if extra else "")
            raise

    templates.env.globals["url_for"] = _url_for
    templates.env.globals["get_flashed_messages"] = lambda **_: []

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc):
        return templates.TemplateResponse(
            request, "404.html", {"is_admin": False}, status_code=404
        )

    return app


app = create_app()


def main():
    uvicorn.run(
        "ui.server:app",
        host="0.0.0.0",
        port=8080,
        reload=MPAC_DEBUG,
        log_level="debug" if MPAC_DEBUG else "info",
    )


if __name__ == "__main__":
    main()
