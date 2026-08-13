from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

router = APIRouter(tags=["pages"])


def _index(request: Request) -> FileResponse:
    return FileResponse(request.app.state.frontend_root / "index.html", media_type="text/html; charset=utf-8")


@router.get("/", include_in_schema=False)
def home(request: Request):
    return _index(request)


@router.get("/components", include_in_schema=False)
def components(request: Request):
    return _index(request)


@router.get("/tasks/{task_id}", include_in_schema=False)
def workspace(task_id: str, request: Request):
    return _index(request)


@router.get("/tasks/{task_id}/{legacy_path:path}", include_in_schema=False)
def legacy_workspace(task_id: str, legacy_path: str, request: Request):
    return _index(request)
