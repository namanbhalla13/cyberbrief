"""Application assembly and the background run executor."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .admin import admin_router
from .ai import init_runner
from .api import (
    auth_router,
    cases_router,
    demo_router,
    exports_router,
    health_router,
    officer_router,
    questions_router,
    recovery_router,
    reviews_router,
    runs_router,
    session_router,
    sources_router,
)
from .assistant import assistant_router
from .bankmail import mail_router
from .bankverify import bank_router
from .consultation import consultation_router
from .core import REPO_ROOT, configure_logging, error_body, get_settings, install_error_handlers
from .investigation import investigation_router
from .workflow import citizen_workflow_router, config_router, officer_workflow_router

# ---- from main.py --------------------------------------------------------------------------------
# FastAPI application: API under /api, built frontend served from the same origin.

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
BACKEND_DIR = Path(__file__).resolve().parents[1]

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    run_migrations()
    runner = init_runner(settings)
    runner.mark_interrupted_on_startup()
    from .events import run_events

    run_events(settings, index=True)   # events left half-done by a restart are picked up again, and the search index is brought up to date
    from .events import EventLoop

    event_loop = EventLoop(settings)
    if settings.app_env != "test":
        event_loop.start()
    from .casework import RetentionScheduler, purge_expired

    purge_expired(settings)
    retention = RetentionScheduler(settings)
    if settings.app_env != "test":
        retention.start()
    yield
    retention.stop()
    event_loop.stop()
    runner.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    show_docs = settings.app_env != "production"
    app = FastAPI(
        title="CyberBrief API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if show_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if show_docs else None,
    )
    install_error_handlers(app)

    if settings.app_env == "development" and settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
        )

    @app.middleware("http")
    async def _limits_and_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > settings.max_request_bytes:
            return JSONResponse(
                error_body(
                    "limit_exceeded",
                    "The request body is larger than this deployment accepts.",
                    "Upload a smaller file (images up to 10 MB; audio up to 2 minutes of 16 kHz WAV).",
                ),
                status_code=413,
            )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    for router in (health_router, session_router, auth_router, cases_router, sources_router,
                   runs_router, reviews_router, questions_router, recovery_router, demo_router,
                   exports_router, officer_router, assistant_router,
                   admin_router, officer_workflow_router, citizen_workflow_router, config_router, investigation_router, consultation_router, bank_router, mail_router):
        app.include_router(router)
    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    dist = FRONTEND_DIST.resolve()
    if not (dist / "index.html").exists():

        @app.get("/", include_in_schema=False)
        async def _no_frontend() -> JSONResponse:
            return JSONResponse({"message": "Frontend not built. Run `npm run build` in frontend/ or use the Vite dev server on port 5173."})

        return

    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path == "openapi.json":
            raise HTTPException(status_code=404, detail="Not found.")
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and dist in candidate.parents:
                return FileResponse(candidate)
        return FileResponse(dist / "index.html")


app = create_app()
