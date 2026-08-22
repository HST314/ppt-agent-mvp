from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse, Response
import hashlib
import html
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from ...errors import ConflictError, NotFoundError, RuntimeUnavailableError, ValidationError
from ...service import TaskService
from ..dependencies import job_service, task_service
from ..jobs import JobService
from ..protocol import exact, json_body

router = APIRouter(prefix="/v1", tags=["tasks"])

STAGES = (
    ("created", "任务/资料", None),
    ("clarification", "澄清", "完成任务创建与资料导入"),
    ("narrative", "叙事结构", "完成澄清回答"),
    ("outline", "逐页大纲", "确认叙事结构"),
    ("sample", "样品", "完成并确认逐页大纲"),
    ("deck", "全稿", "确认当前样品"),
    ("review", "自检与修改", "生成完整演示稿（可选）"),
    ("delivery", "交付", "确定终稿"),
)


@router.get("/tasks")
def list_tasks(jobs: JobService = Depends(job_service)):
    return {"tasks": jobs.list_tasks()}


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, {"task_id", "mode", "target_slide_count"}, {"task_id"})
    return service.create(body["task_id"], body.get("mode", "manual"), body.get("target_slide_count"))


@router.get("/tasks/{task_id}")
def get_task(task_id: str, service: TaskService = Depends(task_service)):
    return service.get(task_id)


@router.get("/tasks/{task_id}/agent-audits")
def get_agent_audits(task_id: str, job_id: str | None = Query(default=None), service: TaskService = Depends(task_service)):
    return {"audits":service.agent_audits(task_id,job_id)}


@router.get("/tasks/{task_id}/shell")
def get_shell(task_id: str, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    state = service.get(task_id)
    summary = service.status_summary(task_id)
    review_available = state["stage"] == "deck" and bool(summary["latest_artifacts"].get("deck"))
    finalization = service.finalization_view(task_id)["current"]
    delivery_available = bool(finalization)
    current_index = next(index for index, item in enumerate(STAGES) if item[0] == state["stage"])
    task_events=service.events(task_id)
    stages = []
    for index, (key, label, prerequisite) in enumerate(STAGES):
        if index == current_index:
            # A completed task has finished the delivery stage too; render it as
            # done (8/8) instead of forever "current".
            item_status = "completed" if key == "delivery" and state["status"] == "completed" else "current"
        elif key == "review" and state["stage"]=="delivery" and finalization and finalization["source"]=="deck":
            item_status = "skipped"
        elif key == "review" and review_available:
            item_status = "available"
        elif key == "delivery" and delivery_available:
            item_status = "available"
        else:
            item_status = "completed" if index < current_index else "locked"
        stages.append({
            "id": key,
            "label": label,
            "status": item_status,
            "lock_reason": f"前置条件：{prerequisite}" if item_status == "locked" else None,
            "href": f"/tasks/{task_id}?stage={key}",
            "revision": state.get("revision") if index==current_index else next((event.get("from",{}).get("revision") for event in reversed(task_events) if event.get("from",{}).get("stage")==key and event.get("to",{}).get("stage")!=key),None),
        })
    return {
        "task": state,
        "summary": summary,
        "stages": stages,
        "active_jobs": jobs.list(task_id, "active"),
        "latest_jobs": jobs.latest_by_operation(task_id),
        "branch": service.store.branch_context(task_id),
    }


@router.get("/settings")
def get_settings(service: TaskService = Depends(task_service)):
    return service.settings_view()


@router.put("/settings")
async def update_settings(request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body=await json_body(request)
    result=service.update_settings(body); jobs.apply_runtime_settings(); return result


@router.get("/tasks/{task_id}/branches")
def get_branches(task_id: str, service: TaskService = Depends(task_service)):
    return service.store.branches(task_id)


@router.post("/tasks/{task_id}/branches", status_code=status.HTTP_201_CREATED)
async def create_branch(task_id: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body=await json_body(request); exact(body,{"branch_id","source_branch","source_revision","switch"},{"branch_id"})
    return jobs.create_branch(task_id,body["branch_id"],body.get("source_branch"),body.get("source_revision"),body.get("switch",True))


@router.post("/tasks/{task_id}/branches/{branch_id}/switch")
async def switch_branch(task_id: str, branch_id: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body=await json_body(request); exact(body,set())
    return jobs.switch_branch(task_id,branch_id)


@router.get("/tasks/{task_id}/input")
def get_input(task_id: str, service: TaskService = Depends(task_service)):
    return service.input_view(task_id)


@router.post("/tasks/{task_id}/input")
async def import_input(task_id: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body = await json_body(request)
    exact(body, {"source", "source_format", "rebuild"}, {"source"})
    result=service.import_input(task_id, body["source"], body.get("source_format", "auto"), body.get("rebuild", False))
    if result.get("clarification",{}).get("status")=="generating":
        try:
            job,_=jobs.create(task_id,"clarification.generate",{},f"clarification-{result['snapshot_hash']}"); result["clarification"]["job_id"]=job["job_id"]
        except RuntimeUnavailableError as error:
            result["clarification"]=service.wait_clarification_for_runtime(task_id,error)
            result["state"]=service.get(task_id)
    return result

@router.post("/tasks/{task_id}/clarifications/retry")
async def retry_clarification(task_id: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body=await json_body(request); exact(body,{"idempotency_key"},{"idempotency_key"})
    if service.input_view(task_id).get("clarification",{}).get("status") not in {"failed","waiting_for_runtime"}: raise ConflictError("仅失败或等待运行时的澄清生成可继续")
    job,_=jobs.create(task_id,"clarification.generate",{},body["idempotency_key"]); return job

@router.post("/tasks/{task_id}/clarifications/fallback")
async def fallback_clarification(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body=await json_body(request); exact(body,{"confirm"},{"confirm"})
    if body["confirm"] is not True: raise ValidationError("使用系统兜底问题需要显式确认")
    return service.use_fallback_clarification(task_id)


def _enqueue_next_clarification_round(task_id: str, result: dict, service: TaskService, jobs: JobService) -> dict:
    """答案合并后若触发了下一轮澄清，像首轮一样交给持久化 Job 生成。"""
    if result.get("status") != "generating":
        return result
    try:
        job, _ = jobs.create(task_id, "clarification.generate", {}, f"clarification-round-{result['clarification_hash'][:16]}")
        result["job_id"] = job["job_id"]
    except RuntimeUnavailableError as error:
        waiting = service.wait_clarification_for_runtime(task_id, error)
        result.update({"status": waiting["status"], "error": waiting.get("error"), "state": service.get(task_id)})
    return result


@router.post("/tasks/{task_id}/clarifications/{question_id}/answer")
async def answer_clarification(task_id: str, question_id: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body = await json_body(request)
    exact(body, {"option", "other"}, {"option"})
    result = service.answer_clarification(task_id, question_id, body)
    return _enqueue_next_clarification_round(task_id, result, service, jobs)

@router.post("/tasks/{task_id}/clarifications/answers")
async def answer_clarifications(task_id: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body = await json_body(request)
    exact(body, {"answers"}, {"answers"})
    result = service.answer_clarifications(task_id, body["answers"], require_complete=True)
    return _enqueue_next_clarification_round(task_id, result, service, jobs)


@router.get("/tasks/{task_id}/planning")
def get_planning(task_id: str, service: TaskService = Depends(task_service)):
    return service.planning_view(task_id)


@router.post("/tasks/{task_id}/narrative")
async def edit_narrative(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, {"markdown", "summary"}, {"markdown"})
    return service.edit_narrative(task_id, body["markdown"], body.get("summary", "直接编辑"))


@router.post("/tasks/{task_id}/narrative/generate")
async def generate_narrative(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, {"prompt", "scope"})
    return service.generate_narrative(task_id, body.get("prompt"), body.get("scope", "all"))


@router.post("/tasks/{task_id}/narrative/confirm")
async def confirm_narrative(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, set())
    return service.confirm_narrative(task_id)


@router.post("/tasks/{task_id}/outline")
async def edit_outline(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, {"markdown", "summary"}, {"markdown"})
    return service.edit_outline(task_id, body["markdown"], body.get("summary", "直接编辑"))


@router.post("/tasks/{task_id}/outline/generate")
async def generate_outline(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, {"prompt", "slide_ids"})
    return service.generate_outline(task_id, body.get("prompt"), body.get("slide_ids"))


@router.post("/tasks/{task_id}/outline/confirm")
async def confirm_outline(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, set())
    return service.confirm_outline(task_id)


@router.post("/tasks/{task_id}/planning/rollback")
async def rollback_planning(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, {"kind", "hash"}, {"kind", "hash"})
    return service.rollback_planning(task_id, body["kind"], body["hash"])


@router.get("/tasks/{task_id}/samples")
def get_samples(task_id: str, service: TaskService = Depends(task_service)):
    return service.sample_view(task_id)


@router.post("/tasks/{task_id}/samples/{action}")
async def change_samples(task_id: str, action: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body = await json_body(request)
    if action == "select":
        exact(body, {"slide_ids", "count"})
        return service.select_samples(task_id, body.get("slide_ids"), body.get("count", 2))
    if action == "generate":
        exact(body, {"prompt"})
        return service.generate_sample(task_id, body.get("prompt"))
    if action == "modify":
        exact(body, {"prompt", "scope", "slide_id", "element_id"}, {"prompt"})
        return service.modify_sample(task_id, body["prompt"], body.get("scope"), body.get("slide_id"), body.get("element_id"))
    if action == "confirm":
        exact(body, {"auto_generate"})
        result=service.confirm_sample(task_id)
        if body.get("auto_generate") is not True:
            return result
        confirmation=result["confirmation"]
        try:
            job,_=jobs.create(task_id,"deck.generate",{},f"deck-{confirmation['confirmed_sample_hash'][:16]}-{confirmation['confirmed_outline_hash'][:16]}")
            result["deck_job"]=job
        except RuntimeUnavailableError as error:
            result["deck_job_error"]=error.public()["error"]
        return result
    raise NotFoundError("接口不存在")


@router.get("/tasks/{task_id}/deck")
def get_deck(task_id: str, service: TaskService = Depends(task_service)):
    return service.deck_view(task_id)


@router.post("/tasks/{task_id}/deck/{action}")
async def change_deck(task_id: str, action: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body = await json_body(request)
    if action == "generate":
        exact(body, set())
        return service.generate_deck(task_id)
    if action == "modify":
        exact(body, {"prompt", "change_type", "scope", "slide_ids", "element_id"}, {"prompt"})
        return service.modify_deck(task_id, body["prompt"], body.get("change_type", "visual"), body.get("scope"), body.get("slide_ids"), body.get("element_id"))
    if action == "rollback":
        exact(body, {"hash"}, {"hash"})
        return service.rollback_deck(task_id, body["hash"])
    if action == "compare":
        exact(body, {"left", "right"}, {"left", "right"})
        return service.compare_decks(task_id, body["left"], body["right"])
    if action == "finalize":
        exact(body, {"deck_hash", "source", "actor", "allow_risk", "risk_rationale"}, {"deck_hash"})
        if jobs.list(task_id,"active"):
            raise ConflictError("仍有生成、修改或修复 Job 运行中，请等待完成或先取消")
        return service.finalize_deck(task_id,body["deck_hash"],body.get("source","deck"),body.get("actor","user"),body.get("allow_risk",False),body.get("risk_rationale",""))
    raise NotFoundError("接口不存在")


@router.get("/tasks/{task_id}/inspection")
def get_inspection(task_id: str, service: TaskService = Depends(task_service)):
    return service.inspection_view(task_id)


@router.post("/tasks/{task_id}/inspection/{action}")
async def change_inspection(task_id: str, action: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    if action == "run":
        exact(body, {"max_rounds", "affected_slide_ids"})
        return service.run_inspection(task_id, body.get("max_rounds", 2), body.get("affected_slide_ids"))
    if action == "mode":
        exact(body, {"mode"}, {"mode"})
        return service.switch_inspection_mode(task_id, body["mode"])
    if action == "autofit":
        exact(body, {"max_rounds"})
        return service.autofit_overflow(task_id, body.get("max_rounds", 2))
    if action == "delivery-gate":
        exact(body, set())
        return service.assert_delivery_gate(task_id)
    raise NotFoundError("接口不存在")


@router.get("/tasks/{task_id}/delivery")
def get_delivery(task_id: str, service: TaskService = Depends(task_service)):
    return service.delivery_view(task_id)


@router.post("/tasks/{task_id}/delivery/{action}")
async def change_delivery(task_id: str, action: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    if action == "confirm":
        exact(body, {"deck_hash", "actor"}, {"deck_hash"})
        return service.confirm_delivery(task_id, body["deck_hash"], body.get("actor", "user"))
    if action == "publish":
        exact(body, set())
        return service.publish_delivery(task_id)
    if action == "derive":
        exact(body, {"delivery_hash", "prompt", "slide_ids"}, {"delivery_hash", "prompt"})
        return service.derive_from_delivery(task_id, body["delivery_hash"], body["prompt"], body.get("slide_ids"))
    if action == "reopen-review":
        exact(body, set())
        return service.reopen_review(task_id)
    raise NotFoundError("接口不存在")


@router.get("/tasks/{task_id}/summary")
def get_summary(task_id: str, service: TaskService = Depends(task_service)):
    return service.status_summary(task_id)


@router.post("/tasks/{task_id}/actions")
async def command(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, {"command_id", "action", "actor", "payload"}, {"command_id", "action"})
    return service.command(task_id, body["command_id"], body["action"], body.get("actor", "system"), body.get("payload"))


@router.get("/tasks/{task_id}/events")
def get_events(task_id: str, limit: int = Query(default=100,ge=1,le=500), before_revision: int | None = Query(default=None,ge=0), service: TaskService = Depends(task_service)):
    events=list(reversed(service.events(task_id)))
    if before_revision is not None: events=[item for item in events if item.get("to",{}).get("revision",0)<before_revision]
    page=events[:limit]
    return {"events":page,"next_revision":page[-1].get("to",{}).get("revision") if len(events)>limit else None}


@router.post("/tasks/{task_id}/versions/compare")
async def compare_versions(task_id: str, request: Request, service: TaskService = Depends(task_service)):
    body = await json_body(request)
    exact(body, {"left", "right"}, {"left", "right"})
    return service.compare(task_id, body["left"], body["right"])


@router.get("/tasks/{task_id}/versions/{digest}")
def get_version(task_id: str, digest: str, service: TaskService = Depends(task_service)):
    return {"hash": digest, "content": service.version(task_id, digest).decode(errors="replace")}


@router.get("/tasks/{task_id}/previews/{digest}", response_class=HTMLResponse, include_in_schema=False)
def get_preview(task_id: str, digest: str, service: TaskService = Depends(task_service)):
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValidationError("预览版本 hash 无效")
    record = next((item for item in service.versions(task_id) if item["hash"] == digest and item["kind"] in {"sample", "deck"}), None)
    if not record or not isinstance(record.get("metadata", {}).get("html"), str):
        raise NotFoundError("预览版本不存在")
    source=record["metadata"]["html"]
    base=f'/v1/tasks/{quote(task_id, safe="")}/preview-assets/{digest}/'
    source=re.sub(r"(<head\b[^>]*>)",lambda match:match.group(1)+f'<base href="{html.escape(base, quote=True)}">',source,count=1,flags=re.I)
    return HTMLResponse(source, headers={
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'self' data: http: https:; "
            "font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'none'; frame-ancestors 'self'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "SAMEORIGIN",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    })


@router.get("/tasks/{task_id}/preview-assets/{digest}/{asset_path:path}", include_in_schema=False)
def get_preview_asset(task_id: str, digest: str, asset_path: str, service: TaskService = Depends(task_service)):
    if len(digest)!=64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValidationError("预览版本 hash 无效")
    if not any(item["hash"]==digest and item["kind"] in {"sample","deck"} for item in service.versions(task_id)):
        raise NotFoundError("预览版本不存在")
    relative=asset_path.removeprefix("resources/")
    path=PurePosixPath(relative)
    if path.is_absolute() or not relative or any(part in {"",".",".."} for part in path.parts):
        raise ValidationError("预览资源路径无效")
    manifest=service.input_view(task_id)["manifest"]
    item=next((entry for entry in manifest.get("resources",[]) if entry.get("uri","").removeprefix("resources://")==relative),None)
    if not item:
        raise NotFoundError("预览资源不存在")
    root=service.store.resource_root(task_id).resolve(); source=(root/relative).resolve()
    if root not in source.parents or not source.is_file():
        raise NotFoundError("预览资源不存在")
    content=source.read_bytes()
    if hashlib.sha256(content).hexdigest()!=item.get("content_hash"):
        raise ConflictError("预览资源内容已变化")
    return Response(content,media_type=item.get("media_type","application/octet-stream"),headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer"})


@router.get("/tasks/{task_id}/versions")
def get_versions(task_id: str, service: TaskService = Depends(task_service)):
    return {"versions": service.versions(task_id)}


@router.post("/tasks/{task_id}/issues/dispositions/batch")
async def dispose_issues(task_id: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body = await json_body(request)
    exact(body, {"issue_ids", "action", "rationale"}, {"issue_ids", "action"})
    if body["action"]=="agent_fix":
        if len(body["issue_ids"])!=1: raise ValidationError("Agent 修复需逐项执行")
        report=service.inspection_view(task_id).get("report") or {}
        job,_=jobs.create(task_id,"inspection.fix",{"issue_id":body["issue_ids"][0],"rationale":body.get("rationale","")},f"inspection-fix-{report.get('hash','none')[:16]}-{body['issue_ids'][0]}")
        return {"job":job}
    return service.dispose_issues(task_id, body["issue_ids"], body["action"], body.get("rationale", ""))


@router.post("/tasks/{task_id}/issues/{issue_id}/disposition")
async def dispose_issue(task_id: str, issue_id: str, request: Request, service: TaskService = Depends(task_service), jobs: JobService = Depends(job_service)):
    body = await json_body(request)
    exact(body, {"action", "rationale", "actor"}, {"action"})
    if body["action"]=="agent_fix":
        report=service.inspection_view(task_id).get("report") or {}
        job,_=jobs.create(task_id,"inspection.fix",{"issue_id":issue_id,"rationale":body.get("rationale","")},f"inspection-fix-{report.get('hash','none')[:16]}-{issue_id}")
        return {"job":job}
    return service.dispose_issue(task_id, issue_id, body["action"], body.get("rationale", ""), body.get("actor", "user"))
