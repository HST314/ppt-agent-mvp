from __future__ import annotations

from fastapi import Request

from ..service import TaskService
from .jobs import JobService


def task_service(request: Request) -> TaskService:
    return request.app.state.task_service


def job_service(request: Request) -> JobService:
    return request.app.state.job_service
