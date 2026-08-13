from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.wsgi import WSGIMiddleware

from ..api import App as LegacyApp
from ..service import TaskService
from ..store import WorkspaceStore
from .errors import install_error_handlers
from .jobs import JobService
from .routes import jobs, pages, tasks


def create_app(
    service: TaskService | None = None,
    *,
    root: str | Path = ".ppt-agent-data",
    frontend_root: str | Path | None = None,
) -> FastAPI:
    service = service or TaskService(WorkspaceStore(root))
    frontend = Path(frontend_root) if frontend_root else Path(__file__).resolve().parents[2] / "frontend"
    frontend = frontend.resolve()
    if not (frontend / "index.html").is_file():
        raise RuntimeError(f"frontend assets missing: {frontend}")
    coordinator = JobService(service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        coordinator.close()

    app = FastAPI(
        title="PPT Agent MVP Web API",
        version="1.0.0-step1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.task_service = service
    app.state.job_service = coordinator
    app.state.frontend_root = frontend

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/legacy/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    @app.get("/healthz", tags=["runtime"])
    def health():
        return {"status": "ok", "stage": "P8", "runtime_ready": True, "web_runtime": "fastapi"}

    app.mount("/static", StaticFiles(directory=frontend / "static", check_dir=True), name="static")
    app.mount("/legacy", WSGIMiddleware(LegacyApp(service)))
    app.include_router(jobs.router)
    app.include_router(tasks.router)
    app.include_router(pages.router)
    install_error_handlers(app)
    return app
