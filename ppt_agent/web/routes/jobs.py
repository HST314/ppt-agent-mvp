from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from ...errors import ValidationError
from ..dependencies import job_service
from ..jobs import JobService, OPERATIONS, TERMINAL
from ..protocol import exact, json_body

router = APIRouter(prefix="/v1", tags=["jobs"])


@router.post("/tasks/{task_id}/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_job(task_id: str, request: Request, jobs: JobService = Depends(job_service)):
    body = await json_body(request)
    exact(body, {"operation", "payload", "idempotency_key"}, {"operation", "idempotency_key"})
    snapshot, _created = jobs.create(task_id, body["operation"], body.get("payload", {}), body["idempotency_key"])
    return snapshot


@router.get("/tasks/{task_id}/jobs")
def list_jobs(task_id: str, status_filter: str | None = Query(default=None, alias="status"), operation: str | None = Query(default=None), limit: int = Query(default=25,ge=1,le=100), before: str | None = Query(default=None), jobs: JobService = Depends(job_service)):
    if status_filter not in {None, "active", "queued", "running", "cancellation_requested", "succeeded", "failed", "cancelled", "interrupted"}:
        raise ValidationError("Job status 筛选无效")
    if operation is not None and operation not in OPERATIONS:
        raise ValidationError("Job operation 筛选无效")
    return jobs.list_page(task_id,status_filter,operation,limit,before)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, jobs: JobService = Depends(job_service)):
    return jobs.get(job_id)


@router.get("/jobs/{job_id}/agent-audits")
def get_job_agent_audits(job_id: str, jobs: JobService = Depends(job_service)):
    snapshot=jobs.get(job_id)
    return {"audits":jobs.service.agent_audits(snapshot["task_id"],job_id)}


@router.get("/jobs/{job_id}/event-history")
def get_job_event_history(
    job_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    jobs: JobService = Depends(job_service),
):
    events = jobs.events(job_id, after)
    return {"events": events[-limit:]}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, jobs: JobService = Depends(job_service)):
    body = await json_body(request)
    exact(body, set())
    return jobs.cancel(job_id)


def _sse(event: dict) -> str:
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.get("/jobs/{job_id}/events")
def job_events(
    job_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    jobs: JobService = Depends(job_service),
):
    if last_event_id:
        try:
            after = max(after, int(last_event_id))
        except ValueError:
            raise ValidationError("Last-Event-ID 无效") from None
    jobs.get(job_id)

    async def stream():
        cursor = after
        idle_ticks = 0
        while True:
            events = jobs.events(job_id, cursor)
            if events:
                idle_ticks = 0
                for event in events:
                    cursor = event["seq"]
                    yield _sse(event)
                if events[-1]["type"] in TERMINAL:
                    return
            else:
                snapshot = jobs.get(job_id)
                if snapshot["status"] in TERMINAL:
                    return
                idle_ticks += 1
                if idle_ticks >= 60:
                    event = jobs.heartbeat(job_id)
                    idle_ticks = 0
                    if event:
                        cursor = event["seq"]
                        yield _sse(event)
            await asyncio.sleep(0.25)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
