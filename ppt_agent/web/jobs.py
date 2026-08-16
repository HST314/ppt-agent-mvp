from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..audit import bind_agent_audit_context
from ..errors import ConflictError, GatewayError, NotFoundError, RuntimeUnavailableError, ValidationError
from ..execution import ExecutionCancelled, ExecutionDeadlineExceeded, execution_scope


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
ACTIVE = {"queued", "running", "cancellation_requested"}
OPERATIONS = {
    "clarification.generate",
    "narrative.generate",
    "outline.generate",
    "samples.generate",
    "samples.modify",
    "deck.generate",
    "deck.modify",
    "inspection.run",
}
OPERATION_STAGES = {
    "clarification.generate": {"clarification"},
    "narrative.generate": {"clarification", "narrative"},
    "outline.generate": {"narrative", "outline"},
    "samples.generate": {"outline", "sample"},
    "samples.modify": {"outline", "sample"},
    "deck.generate": {"sample", "deck"},
    "deck.modify": {"deck", "review"},
    "inspection.run": {"deck", "review"},
}
OPERATION_BUDGET_SECONDS = {"clarification.generate":180,"narrative.generate":240,"outline.generate":240,"samples.generate":300,"samples.modify":300,"deck.generate":600,"deck.modify":600,"inspection.run":300}


class ActiveJobConflict(ConflictError):
    def __init__(self, job_id: str):
        super().__init__("任务已有活动 Job")
        self.job_id = job_id

    def public(self) -> dict:
        payload = super().public()
        payload["error"]["active_job_id"] = self.job_id
        return payload


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(operation: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


class JobService:
    """Persistent, task-scoped background job coordinator.

    Domain artifacts remain authoritative in TaskService. Job records only retain
    execution metadata and references needed to refresh the business view.
    """

    def __init__(self, service, *, executor: ThreadPoolExecutor | None = None, defer_queued_recovery: bool = False):
        self.service = service
        self.store = service.store
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="ppt-job")
        self._guard = threading.RLock()
        self._submitted: set[str] = set()
        self._recovered_queued: list[tuple[str, str]] = []
        self._recover()
        if not defer_queued_recovery:
            self.resume_recovered_queued()

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _root(self, task_id: str) -> Path:
        self.store.checkpoint(task_id)
        root = self.store._task(task_id) / "jobs"
        root.mkdir(exist_ok=True)
        return root

    def _record_path(self, task_id: str, job_id: str) -> Path:
        if not job_id.startswith("job_") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in job_id):
            raise ValidationError("job_id 格式无效")
        return self._root(task_id) / f"{job_id}.json"

    def _event_path(self, task_id: str, job_id: str) -> Path:
        return self._root(task_id) / f"{job_id}.events.jsonl"

    def _read(self, task_id: str, job_id: str) -> dict[str, Any]:
        path = self._record_path(task_id, job_id)
        pending = self._root(task_id) / f"{job_id}.pending-event.json"
        if pending.exists():
            self._finish_pending(pending)
        if not path.exists():
            raise NotFoundError("Job 不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, record: dict[str, Any]) -> None:
        self.store.atomic_json(self._record_path(record["task_id"], record["job_id"]), record)

    def _records(self, task_id: str) -> list[dict[str, Any]]:
        root = self._root(task_id)
        records = []
        for path in root.glob("job_*.json"):
            if path.name.endswith(".pending-event.json"):
                continue
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(records, key=lambda item: (item["created_at"], item["job_id"]))

    def _append_event(self, record: dict[str, Any], event_type: str, **values: Any) -> dict[str, Any]:
        record["last_seq"] += 1
        event = {
            "seq": record["last_seq"],
            "job_id": record["job_id"],
            "type": event_type,
            "progress": record.get("progress"),
            "step": record.get("current_step"),
            "message": values.pop("message", None),
            "at": _now(),
            **values,
        }
        pending = self._root(record["task_id"]) / f"{record['job_id']}.pending-event.json"
        self.store.atomic_json(pending, {"record": record, "event": event})
        self._finish_pending(pending)
        return event

    def _finish_pending(self, pending: Path) -> None:
        value = json.loads(pending.read_text(encoding="utf-8"))
        record, event = value["record"], value["event"]
        path = self._event_path(record["task_id"], record["job_id"])
        existing = {
            item["seq"]: item
            for item in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
        } if path.exists() else {}
        if event["seq"] in existing and existing[event["seq"]] != event:
            raise OSError("Job 事件序号冲突")
        if event["seq"] not in existing:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        self._write(record)
        pending.unlink(missing_ok=True)

    def _recover(self) -> None:
        if not self.store.root.exists():
            return
        queued: list[tuple[str, str]] = []
        for task_path in self.store.root.iterdir():
            if not task_path.is_dir() or not (task_path / "checkpoint.json").exists():
                continue
            jobs = task_path / "jobs"
            if not jobs.is_dir():
                continue
            for pending in jobs.glob("job_*.pending-event.json"):
                try:
                    self._finish_pending(pending)
                except (OSError, KeyError, json.JSONDecodeError):
                    logging.exception("Job WAL recovery failed", extra={"pending": str(pending)})
            for path in jobs.glob("job_*.json"):
                if path.name.endswith(".pending-event.json"):
                    continue
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if record.get("status") in {"running", "cancellation_requested"}:
                    record.update(status="interrupted", current_step="recovery_required", finished_at=_now())
                    record["error"] = {
                        "code": "job_interrupted",
                        "message": "服务重启时执行结果未知，未自动重试",
                    }
                    with self._guard:
                        self._append_event(record, "interrupted", message=record["error"]["message"])
                elif record.get("status") == "queued":
                    queued.append((record["task_id"], record["job_id"]))
        self._recovered_queued.extend(queued)

    def resume_recovered_queued(self) -> None:
        """Resume queued records only after the application probes readiness."""
        with self._guard:
            queued, self._recovered_queued = self._recovered_queued, []
        for task_id, job_id in queued:
            self._submit(task_id, job_id)

    @staticmethod
    def _validate_payload(operation: str, payload: dict[str, Any]) -> None:
        if operation not in OPERATIONS:
            raise ValidationError("不支持的 Job 操作")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须为对象")
        allowed = {
            "clarification.generate": set(),
            "narrative.generate": {"prompt", "scope"},
            "outline.generate": {"prompt", "slide_ids"},
            "samples.generate": {"prompt"},
            "samples.modify": {"prompt", "scope", "slide_id", "element_id"},
            "deck.generate": set(),
            "deck.modify": {"prompt", "change_type", "scope", "slide_ids", "element_id"},
            "inspection.run": {"max_rounds", "affected_slide_ids"},
        }[operation]
        required = {"prompt"} if operation in {"samples.modify", "deck.modify"} else set()
        if set(payload) - allowed or required - set(payload):
            raise ValidationError("Job payload 字段无效")

    def create(self, task_id: str, operation: str, payload: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], bool]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValidationError("idempotency_key 无效")
        self._validate_payload(operation, payload)
        state = self.service.get(task_id)
        fingerprint = _fingerprint(operation, payload)
        with self._guard, self.store.lock(task_id):
            records = self._records(task_id)
            previous = next((item for item in records if item["idempotency_key"] == idempotency_key), None)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ConflictError("相同 idempotency_key 对应了不同请求")
                return self.public(previous), False
            self.service.require_runtime_ready()
            if state["status"] in {"paused", "cancelled", "failed", "completed"}:
                raise ConflictError("当前任务状态不能启动长任务")
            if state["stage"] not in OPERATION_STAGES[operation]:
                raise ConflictError("当前任务阶段不能执行该长任务")
            active = next((item for item in records if item["status"] in ACTIVE), None)
            if active:
                raise ActiveJobConflict(active["job_id"])
            job_id = f"job_{uuid.uuid4().hex}"
            record = {
                "job_id": job_id,
                "task_id": task_id,
                "operation": operation,
                "payload": payload,
                "idempotency_key": idempotency_key,
                "fingerprint": fingerprint,
                "status": "queued",
                "progress": 0,
                "current_step": "queued",
                "last_seq": 0,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
                "cancellation_requested": False,
                "deadline_seconds": OPERATION_BUDGET_SECONDS[operation],
                "deadline_at": None,
            }
            self._write(record)
            self._append_event(record, "queued", message="任务已进入执行队列")
            self._submit(task_id, job_id)
            return self.public(record), True

    def _submit(self, task_id: str, job_id: str) -> None:
        key = f"{task_id}:{job_id}"
        with self._guard:
            if key in self._submitted:
                return
            self._submitted.add(key)
        self.executor.submit(self._run, task_id, job_id, key)

    def _run(self, task_id: str, job_id: str, key: str) -> None:
        try:
            with self._guard:
                record = self._read(task_id, job_id)
                if record["status"] != "queued":
                    return
                if record["cancellation_requested"]:
                    record.update(status="cancelled", current_step="cancelled", finished_at=_now())
                    self._append_event(record, "cancelled", message="任务已取消")
                    return
                started = datetime.now(timezone.utc)
                record.update(status="running", progress=None, current_step="domain_operation", started_at=started.isoformat(), deadline_at=(started + timedelta(seconds=record.get("deadline_seconds", OPERATION_BUDGET_SECONDS[record["operation"]]))).isoformat())
                self._append_event(record, "started", message="业务操作已开始")
            deadline = time.monotonic() + record.get("deadline_seconds", OPERATION_BUDGET_SECONDS[record["operation"]])
            def publish_progress(step, message):
                with self._guard:
                    current = self._read(task_id, job_id)
                    if current["status"] not in ACTIVE:
                        return
                    current["current_step"] = step
                    self._append_event(current, "checkpoint", message=message or step)
            with execution_scope(lambda: self._read(task_id, job_id).get("cancellation_requested", False), deadline, publish_progress):
                # Readiness can change after enqueue or while a queued job is being
                # recovered. Never cross the model boundary without a fresh gate.
                try:
                    self.service.require_runtime_ready()
                except RuntimeUnavailableError as error:
                    if record["operation"] == "clarification.generate":
                        self.service.fail_clarification_for_runtime(task_id, error)
                    raise
                with bind_agent_audit_context(task_id=task_id, job_id=job_id):
                    result = self._invoke(record["operation"], task_id, record["payload"])
                self.service.record_runtime_success()
                from ..execution import checkpoint
                checkpoint()
            state = self.service.get(task_id)
            try:
                artifacts = self.service.status_summary(task_id).get("latest_artifacts", {})
            except Exception:
                artifacts = {}
            reference = {
                "task_id": task_id,
                "stage": state["stage"],
                "status": state["status"],
                "revision": state.get("revision"),
                "artifacts": artifacts,
            }
            with self._guard:
                record = self._read(task_id, job_id)
                if record.get("cancellation_requested"):
                    raise ExecutionCancelled()
                record.update(
                    status="succeeded",
                    progress=100,
                    current_step="completed",
                    finished_at=_now(),
                    result=reference,
                    error=None,
                )
                self._append_event(record, "succeeded", message="业务操作已完成")
        except Exception as error:
            if isinstance(error, GatewayError):
                self.service.record_runtime_failure(error)
            if record.get("operation") == "clarification.generate":
                try:
                    # Recovery is deliberately outside the expired execution
                    # scope, otherwise its own task commit would be rejected.
                    with execution_scope(None, None):
                        self.service.recover_clarification_failure(task_id, error)
                except Exception:
                    logging.exception("clarification recovery failed", extra={"job_id": job_id})
            with self._guard:
                try:
                    record = self._read(task_id, job_id)
                    if isinstance(error, ExecutionCancelled):
                        record.update(status="cancelled", progress=None, current_step="cancelled", finished_at=_now(), error=None)
                        self._append_event(record, "cancelled", message="任务已取消，未提交业务结果")
                        return
                    public = error.public()["error"] if hasattr(error, "public") else {
                        "code": "internal_error",
                        "message": "后台任务执行失败",
                        "diagnostic_id": uuid.uuid4().hex,
                    }
                    if isinstance(error, OSError):
                        public = {"code":"job_persistence_error","message":"Job 持久化失败，请重试","errno":error.errno}
                    if not hasattr(error, "public"):
                        logging.exception(
                            "background Job failed",
                            extra={"job_id": job_id, "task_id": task_id, "diagnostic_id": public.get("diagnostic_id")},
                        )
                    if isinstance(error, ExecutionDeadlineExceeded):
                        public = {"code":"stage_deadline_exceeded","message":"阶段执行超过硬截止时间"}
                    record.update(
                        status="failed",
                        current_step="failed",
                        finished_at=_now(),
                        error=public,
                    )
                    self._append_event(record, "failed", message=public["message"], error=public)
                except Exception:
                    logging.exception("failed to persist terminal Job state", extra={"job_id": job_id, "task_id": task_id})
        finally:
            with self._guard:
                self._submitted.discard(key)

    def _invoke(self, operation: str, task_id: str, payload: dict[str, Any]) -> Any:
        if operation == "clarification.generate":
            return self.service.generate_clarification(task_id)
        if operation == "narrative.generate":
            return self.service.generate_narrative(task_id, payload.get("prompt"), payload.get("scope", "all"))
        if operation == "outline.generate":
            return self.service.generate_outline(task_id, payload.get("prompt"), payload.get("slide_ids"))
        if operation == "samples.generate":
            return self.service.generate_sample(task_id, payload.get("prompt"))
        if operation == "samples.modify":
            return self.service.modify_sample(task_id, payload["prompt"], payload.get("scope"), payload.get("slide_id"), payload.get("element_id"))
        if operation == "deck.generate":
            return self.service.generate_deck(task_id)
        if operation == "deck.modify":
            return self.service.modify_deck(task_id, payload["prompt"], payload.get("change_type", "visual"), payload.get("scope"), payload.get("slide_ids"), payload.get("element_id"))
        if operation == "inspection.run":
            return self.service.run_inspection(task_id, payload.get("max_rounds", 2), payload.get("affected_slide_ids"))
        raise ValidationError("不支持的 Job 操作")

    @staticmethod
    def public(record: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "job_id", "task_id", "operation", "status", "progress", "current_step", "last_seq",
            "created_at", "started_at", "finished_at", "result", "error", "cancellation_requested",
            "deadline_seconds", "deadline_at",
        )
        return {key: record.get(key) for key in keys}

    def get(self, job_id: str) -> dict[str, Any]:
        if not job_id.startswith("job_"):
            raise ValidationError("job_id 格式无效")
        for task_path in self.store.root.iterdir():
            path = task_path / "jobs" / f"{job_id}.json"
            if path.exists():
                return self.public(self._read(task_path.name, job_id))
        raise NotFoundError("Job 不存在")

    def list(self, task_id: str, status: str | None = None) -> list[dict[str, Any]]:
        records = self._records(task_id)
        if status == "active":
            records = [item for item in records if item["status"] in ACTIVE]
        elif status:
            records = [item for item in records if item["status"] == status]
        return [self.public(item) for item in records]

    def latest_by_operation(self, task_id: str) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for record in reversed(self._records(task_id)):
            latest.setdefault(record["operation"], record)
        return [self.public(record) for record in sorted(latest.values(), key=lambda item: (item["created_at"], item["job_id"]))]

    def cancel(self, job_id: str) -> dict[str, Any]:
        snapshot = self.get(job_id)
        # Same task lock as domain publication: cancellation and commit have a
        # single observable order and can no longer cross after_prepare.
        with self._guard, self.store.lock(snapshot["task_id"]):
            record = self._read(snapshot["task_id"], job_id)
            if record["status"] in TERMINAL:
                return self.public(record)
            record["cancellation_requested"] = True
            if record["status"] == "queued":
                record.update(status="cancelled", current_step="cancelled", finished_at=_now())
                self._append_event(record, "cancelled", message="排队任务已取消")
            else:
                record.update(status="cancellation_requested", current_step="cancellation_requested")
                self._append_event(record, "checkpoint", message="已请求取消；正在等待安全停止点")
            return self.public(record)

    def events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        snapshot = self.get(job_id)
        path = self._event_path(snapshot["task_id"], job_id)
        if not path.exists():
            return []
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        return [event for event in events if event["seq"] > after]

    def heartbeat(self, job_id: str) -> dict[str, Any] | None:
        snapshot = self.get(job_id)
        if snapshot["status"] in TERMINAL:
            return None
        with self._guard:
            record = self._read(snapshot["task_id"], job_id)
            return self._append_event(record, "heartbeat", message="连接正常")

    def list_tasks(self) -> list[dict[str, Any]]:
        tasks = []
        for path in self.store.root.iterdir():
            checkpoint = path / "checkpoint.json"
            if not path.is_dir() or not checkpoint.exists():
                continue
            try:
                state = json.loads(checkpoint.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tasks.append({**state, "updated_at": datetime.fromtimestamp(checkpoint.stat().st_mtime, timezone.utc).isoformat()})
        return sorted(tasks, key=lambda item: item["updated_at"], reverse=True)
