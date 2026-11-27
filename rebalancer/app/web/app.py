"""FastAPI construction, security headers, and application lifespan."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..application import RebalancerApplication
from ..presentation import format_eastern_time
from .auth import AuthenticationService
from .routes import build_router

_WEB_ROOT = Path(__file__).resolve().parent

_CSP = (
    "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "object-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'"
)


class ApplicationLifecycle(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def create_web_app(
    application: RebalancerApplication,
    auth: AuthenticationService,
    *,
    lifecycle: Optional[ApplicationLifecycle] = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if lifecycle is not None:
            await lifecycle.start()
        try:
            yield
        finally:
            if lifecycle is not None:
                await lifecycle.stop()

    app = FastAPI(
        title="Zehnlabs Rebalancer",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(_WEB_ROOT / "templates"))
    templates.env.filters["eastern_time"] = format_eastern_time
    app.mount("/static", StaticFiles(directory=str(_WEB_ROOT / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path != "/healthz" and not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers["Content-Security-Policy"] = _CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Permissions-Policy"] = (
                "accelerometer=(), camera=(), geolocation=(), microphone=(), payment=(), usb=()"
            )
            response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        try:
            healthy = application.state.health_check()
        except Exception:
            healthy = False
        return JSONResponse(
            {"status": "ok" if healthy else "unavailable"},
            status_code=200 if healthy else 503,
        )

    @app.exception_handler(Exception)
    async def safe_error(_request: Request, _exc: Exception) -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><title>Request failed</title>"
            "<h1>Request could not be completed</h1>"
            "<p>Try again or inspect the service logs.</p>",
            status_code=500,
        )

    app.include_router(build_router(application, auth, templates))
    return app
