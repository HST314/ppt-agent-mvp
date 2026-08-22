from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
import logging
from pathlib import Path
import threading

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..service import TaskService
from ..store import WorkspaceStore
from .errors import install_error_handlers
from .jobs import JobService
from .routes import jobs, pages, tasks
from .assets import FRONTEND_BUILD, backend_commit


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
    coordinator = JobService(service, defer_queued_recovery=True, defer_recovery_scan=True)
    shutdown=threading.Event()
    bootstrap={"recovery":"starting","runtime":"starting"}

    def initialize_background():
        try:
            coordinator.initialize_recovery()
            bootstrap["recovery"]="ready"
            capabilities=service.initialize_runtime()
            # Startup completion and provider capability are separate signals.
            # A later background probe can recover capability without mutating
            # this one-shot bootstrap state.
            bootstrap["runtime"]="ready"
            if capabilities.get("ready") and not shutdown.is_set(): coordinator.resume_recovered_queued()
        except Exception:
            pending="recovery" if bootstrap["recovery"]=="starting" else "runtime"
            bootstrap[pending]="failed"
            logging.exception("background startup initialization failed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        thread=threading.Thread(target=initialize_background,name="ppt-startup",daemon=True)
        thread.start()
        yield
        shutdown.set()
        thread.join(timeout=1)
        coordinator.close()

    app = FastAPI(
        title="PPT Agent MVP Web API",
        version="1.0.0-step2",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.task_service = service
    app.state.job_service = coordinator
    app.state.frontend_root = frontend
    app.state.frontend_build = FRONTEND_BUILD

    def startup_status():
        statuses=set(bootstrap.values())
        if "failed" in statuses: return "failed"
        if "starting" in statuses: return "starting"
        return "ready"

    def runtime_payload():
        capabilities=service.runtime_health()
        config_summary=service.runtime_config_summary()
        startup=startup_status()
        ready=capabilities["ready"] and startup=="ready"
        return {
            "status": "ok" if ready else "unavailable",
            "stage": "P8",
            "runtime_ready": ready,
            "web_runtime": "fastapi",
            "frontend_build": FRONTEND_BUILD,
            "backend_commit": backend_commit(),
            "config_summary_sha256": hashlib.sha256(json.dumps(config_summary,sort_keys=True,separators=(",",":")).encode()).hexdigest(),
            "clarification_mode":"model" if service.clarifier is not None else "fake",
            "model_capabilities":capabilities,
            "startup_status":startup,
            "startup_components":dict(bootstrap),
        }

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        is_preview = "/previews/" in request.url.path
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if is_preview else "DENY"
        if "Content-Security-Policy" not in response.headers:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
            )
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if request.query_params.get("v") == FRONTEND_BUILD
                else "no-cache"
            )
        return response

    @app.get("/livez", tags=["runtime"])
    def live():
        payload=runtime_payload()
        return {key:payload[key] for key in ("status","web_runtime","frontend_build","backend_commit","config_summary_sha256")} | {"status":"ok"}

    @app.get("/readyz", tags=["runtime"])
    def ready():
        payload=runtime_payload()
        return JSONResponse(status_code=200 if payload["runtime_ready"] else 503,content=payload)

    @app.get("/healthz", tags=["runtime"])
    def health():
        payload=runtime_payload()
        return JSONResponse(status_code=200 if payload["runtime_ready"] else 503,content=payload)

    @app.get("/v1/runtime/status", tags=["runtime"])
    def runtime_status():
        return runtime_payload()

    @app.post("/v1/runtime/recheck", tags=["runtime"])
    def recheck_runtime():
        capabilities=service.initialize_runtime()
        bootstrap["runtime"]="ready"
        return runtime_payload()

    @app.get("/v1/runtime/probes", tags=["runtime"])
    def runtime_probes(limit: int = Query(default=20,ge=1,le=100)):
        return {"probes":service.runtime_probes(limit)}

    app.mount("/static", StaticFiles(directory=frontend / "static", check_dir=True), name="static")
    app.include_router(jobs.router)
    app.include_router(tasks.router)
    app.include_router(pages.router)
    install_error_handlers(app)
    return app
