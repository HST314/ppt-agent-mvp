from __future__ import annotations

from fastapi import APIRouter, Request

from ..assets import index_response

router = APIRouter(tags=["pages"])


def _index(request: Request):
    return index_response(request.app.state.frontend_root)


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
