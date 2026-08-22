from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import portalocker

from ..audit import bind_agent_audit_context
from ..errors import ConflictError, GatewayError, GatewayUnknownResult, NotFoundError, RuntimeUnavailableError, ValidationError
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
    "inspection.fix",
    "delivery.publish",
}
OPERATION_STAGES = {
    "clarification.generate": {"clarification"},
    "narrative.generate": {"clarification", "narrative"},
    "outline.generate": {"narrative", "outline"},
    "samples.generate": {"outline", "sample"},
    "samples.modify": {"outline", "sample"},
    "deck.generate": {"deck"},
    "deck.modify": {"deck", "review"},
    "inspection.run": {"deck", "review"},
    "inspection.fix": {"review"},
    "delivery.publish": {"delivery"},
}
OPERATION_BUDGET_SECONDS = {
    "clarification.generate": 180,
    "narrative.generate": 240,
    "outline.generate": 240,
    "samples.generate": 630,
    "samples.modify": 630,
    "deck.generate": 630,
    "deck.modify": 630,
    "inspection.run": 630,
    "inspection.fix": 630,
    "delivery.publish": 180,
}
GENERATION_OPERATIONS = {
    "clarification.generate", "narrative.generate", "outline.generate",
    "samples.generate", "samples.modify", "deck.generate", "deck.modify", "inspection.fix",
}
METRIC_KEYS = {
    "stage", "agent_step", "max_steps", "provider_calls", "max_provider_calls",
    "tool_calls", "max_tool_calls", "tool_name", "tool_path", "tool_failed",
}
EVENT_LOCK_TIMEOUT_SECONDS = 10
EVENT_PERSISTENCE_ERRORS = (
    OSError,
    json.JSONDecodeError,
    portalocker.exceptions.LockException,
)


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

    def __init__(
        self,
        service,
        *,
        executor: ThreadPoolExecutor | None = None,
        defer_queued_recovery: bool = False,
        defer_recovery_scan: bool = False,
        runtime_recovery_delays: tuple[float, ...] = (0.25, 1, 4, 16, 30, 30),
    ):
        self.service = service
        self.store = service.store
        self.operation_budgets = dict(OPERATION_BUDGET_SECONDS)
        generation_timeout = max(
            (
                getattr(gateway, "job_timeout_seconds", 0)
                for gateway in (getattr(service, "clarifier", None), getattr(service, "generator", None), getattr(service, "builder", None))
            ),
            default=0,
        )
        inspection_timeout = getattr(getattr(service, "inspector", None), "job_timeout_seconds", 0)
        if generation_timeout:
            self.operation_budgets.update({operation: generation_timeout for operation in GENERATION_OPERATIONS})
        if inspection_timeout:
            self.operation_budgets.update({operation:inspection_timeout for operation in {"inspection.run","inspection.fix"}})
        self.apply_runtime_settings()
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="ppt-job")
        self._guard = threading.RLock()
        self._submitted: set[str] = set()
        self._recovered_queued: list[tuple[str, str]] = []
        self._recovery_initialized=False
        self._recovery_complete=False
        self._runtime_recovery_delays=runtime_recovery_delays
        self._runtime_recovery_stop=threading.Event()
        self._runtime_recovery_guard=threading.Lock()
        self._runtime_recovery_thread: threading.Thread | None=None
        if not defer_recovery_scan:
            self.initialize_recovery()
        if not defer_queued_recovery and self._recovery_complete:
            self.resume_recovered_queued()

    def apply_runtime_settings(self) -> None:
        configured=self.service.job_timeouts() if hasattr(self.service,"job_timeouts") else {}
        generation=configured.get("generation_timeout_seconds")
        inspection=configured.get("inspection_timeout_seconds")
        delivery=configured.get("delivery_timeout_seconds")
        if generation: self.operation_budgets.update({operation:generation for operation in GENERATION_OPERATIONS})
        if inspection: self.operation_budgets.update({operation:inspection for operation in {"inspection.run","inspection.fix"}})
        if delivery: self.operation_budgets["delivery.publish"]=delivery

    def initialize_recovery(self) -> None:
        with self._guard:
            if self._recovery_initialized: return
            self._recovery_initialized=True
        try:
            self._recover()
        except Exception:
            with self._guard:
                self._recovery_initialized=False
            raise
        with self._guard:
            self._recovery_complete=True

    def close(self) -> None:
        self._runtime_recovery_stop.set()
        thread=self._runtime_recovery_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.25)
        self.executor.shutdown(wait=False, cancel_futures=False)

    def schedule_runtime_recovery(self) -> None:
        """Probe capability after an unknown Job without replaying that Job."""
        if not self._runtime_recovery_delays or self._runtime_recovery_stop.is_set():
            return
        with self._runtime_recovery_guard:
            if self._runtime_recovery_thread is not None and self._runtime_recovery_thread.is_alive():
                return
            self._runtime_recovery_thread=threading.Thread(
                target=self._recover_runtime,
                name="ppt-runtime-recovery",
                daemon=True,
            )
            self._runtime_recovery_thread.start()

    def _recover_runtime(self) -> None:
        for delay in self._runtime_recovery_delays:
            if self._runtime_recovery_stop.wait(delay):
                return
            if self.service.runtime_health().get("ready"):
                return
            try:
                capabilities=self.service.initialize_runtime()
            except Exception:
                logging.exception("background runtime capability probe failed")
                continue
            if capabilities.get("ready"):
                return

    def _root(self, task_id: str) -> Path:
        task_root = self.store._task(task_id)
        if not (task_root / "checkpoint.json").exists():
            raise NotFoundError("任务不存在")
        root = task_root / "jobs"
        root.mkdir(exist_ok=True)
        return root

    def _record_path(self, task_id: str, job_id: str) -> Path:
        if not job_id.startswith("job_") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in job_id):
            raise ValidationError("job_id 格式无效")
        return self._root(task_id) / f"{job_id}.json"

    def _event_path(self, task_id: str, job_id: str) -> Path:
        return self._root(task_id) / f"{job_id}.events.jsonl"

    @contextmanager
    def _event_lock(self, task_id: str, job_id: str):
        """Serialize the whole event WAL transaction on POSIX and Windows."""
        path = self._root(task_id) / f"{job_id}.events.lock"
        with portalocker.Lock(
            str(path),
            mode="a+b",
            timeout=EVENT_LOCK_TIMEOUT_SECONDS,
            check_interval=0.05,
        ):
            yield

    @staticmethod
    def _history_warning(
        record: dict[str, Any],
        *,
        code: str,
        message: str,
        recovered: bool,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = record.get("event_history_warning") or {}
        warning = {
            "code": code,
            "message": message,
            "diagnostic_id": previous.get("diagnostic_id") or uuid.uuid4().hex,
            "recovered": recovered,
            "at": _now(),
        }
        if details:
            warning["details"] = details
        return warning

    @staticmethod
    def _backup_once(path: Path, reason: str) -> Path:
        backup = path.with_name(f"{path.name}.{reason}.bak")
        if backup.exists():
            return backup
        raw = path.read_bytes()
        try:
            with open(backup, "xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        return backup

    @staticmethod
    def _write_event_history(path: Path, events: list[dict[str, Any]]) -> None:
        raw = "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in events
        ).encode("utf-8")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_event_history_locked(
        self,
        task_id: str,
        job_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return a strictly increasing history and repair legacy corruption."""
        path = self._event_path(task_id, job_id)
        if not path.exists():
            return [], []
        events: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        seen_original: dict[int, dict[str, Any]] = {}
        last_seq = 0
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                repairs.append({"reason": "invalid_json", "line": line_number})
                continue
            seq = event.get("seq") if isinstance(event, dict) else None
            if (
                not isinstance(event, dict)
                or isinstance(seq, bool)
                or not isinstance(seq, int)
                or seq < 1
                or event.get("job_id") != job_id
            ):
                repairs.append({"reason": "invalid_event", "line": line_number})
                continue
            if seq in seen_original and seen_original[seq] == event:
                repairs.append({"reason": "duplicate_event", "line": line_number, "original_seq": seq})
                continue
            seen_original.setdefault(seq, event)
            if seq <= last_seq:
                original_seq = seq
                event = dict(event)
                event["seq"] = last_seq + 1
                event["storage_repair"] = {
                    "reason": "sequence_conflict",
                    "original_seq": original_seq,
                }
                repairs.append(
                    {
                        "reason": "sequence_conflict",
                        "line": line_number,
                        "original_seq": original_seq,
                        "assigned_seq": event["seq"],
                    }
                )
            last_seq = event["seq"]
            events.append(event)
        if repairs:
            self._backup_once(path, "recovery")
            self._write_event_history(path, events)
        return events, repairs

    def _quarantine_pending_locked(
        self,
        task_id: str,
        job_id: str,
        pending: Path,
        error: Exception,
    ) -> dict[str, Any] | None:
        self._backup_once(pending, "corrupt")
        pending.unlink(missing_ok=True)
        path = self._record_path(task_id, job_id)
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        record["event_history_warning"] = self._history_warning(
            record,
            code="job_event_history_degraded",
            message="事件历史 WAL 已隔离；业务状态仍可查询",
            recovered=False,
            details={"exception_type": type(error).__name__},
        )
        self._write(record)
        return record

    def _finish_pending_locked(
        self,
        pending: Path,
        expected_task_id: str | None = None,
        expected_job_id: str | None = None,
    ) -> dict[str, Any]:
        value = json.loads(pending.read_text(encoding="utf-8"))
        record, event = dict(value["record"]), dict(value["event"])
        task_id, job_id = record["task_id"], record["job_id"]
        if (
            event.get("job_id") != job_id
            or (expected_task_id is not None and task_id != expected_task_id)
            or (expected_job_id is not None and job_id != expected_job_id)
        ):
            raise ValueError("pending Job 事件归属无效")

        history, history_repairs = self._read_event_history_locked(task_id, job_id)
        maximum_seq = max((item["seq"] for item in history), default=0)
        existing = {item["seq"]: item for item in history}
        original_seq = event.get("seq")
        if isinstance(original_seq, bool) or not isinstance(original_seq, int) or original_seq < 1:
            raise ValueError("pending Job 事件序号无效")

        pending_repaired = False
        if original_seq in existing and existing[original_seq] != event:
            event["seq"] = maximum_seq + 1
            event["storage_repair"] = {
                "reason": "pending_sequence_conflict",
                "original_seq": original_seq,
            }
            pending_repaired = True
        elif original_seq <= maximum_seq and original_seq not in existing:
            event["seq"] = maximum_seq + 1
            event["storage_repair"] = {
                "reason": "pending_out_of_order",
                "original_seq": original_seq,
            }
            pending_repaired = True

        if pending_repaired:
            self._backup_once(pending, "recovery")
        if event["seq"] not in existing:
            path = self._event_path(task_id, job_id)
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

        record_path = self._record_path(task_id, job_id)
        if record_path.exists():
            persisted = json.loads(record_path.read_text(encoding="utf-8"))
            if persisted.get("last_seq", 0) > record.get("last_seq", 0) or persisted.get("status") in TERMINAL:
                record = persisted
        record["last_seq"] = max(record.get("last_seq", 0), maximum_seq, event["seq"])
        if history_repairs or pending_repaired:
            record["event_history_warning"] = self._history_warning(
                record,
                code="job_event_history_repaired",
                message="事件历史已自动修复；业务状态可继续使用",
                recovered=True,
                details={
                    "history_repairs": len(history_repairs),
                    "pending_resequenced": pending_repaired,
                },
            )
        self._write(record)
        pending.unlink(missing_ok=True)
        return record

    def _repair_event_history(self, task_id: str, job_id: str) -> list[dict[str, Any]]:
        with self._event_lock(task_id, job_id):
            path = self._record_path(task_id, job_id)
            pending = self._root(task_id) / f"{job_id}.pending-event.json"
            if pending.exists():
                try:
                    self._finish_pending_locked(pending, task_id, job_id)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    self._quarantine_pending_locked(task_id, job_id, pending, error)
            events, repairs = self._read_event_history_locked(task_id, job_id)
            if repairs and path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                record["last_seq"] = max(record.get("last_seq", 0), max((item["seq"] for item in events), default=0))
                record["event_history_warning"] = self._history_warning(
                    record,
                    code="job_event_history_repaired",
                    message="事件历史已自动修复；业务状态可继续使用",
                    recovered=True,
                    details={"history_repairs": len(repairs)},
                )
                self._write(record)
            return events

    def _read(self, task_id: str, job_id: str) -> dict[str, Any]:
        path = self._record_path(task_id, job_id)
        pending = self._root(task_id) / f"{job_id}.pending-event.json"
        with self._event_lock(task_id, job_id):
            if pending.exists():
                try:
                    self._finish_pending_locked(pending, task_id, job_id)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    self._quarantine_pending_locked(task_id, job_id, pending, error)
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
                records.append(self._read(task_id, path.stem))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(records, key=lambda item: (item["created_at"], item["job_id"]))

    def _append_event(self, record: dict[str, Any], event_type: str, **values: Any) -> dict[str, Any]:
        with self._event_lock(record["task_id"], record["job_id"]):
            pending = self._root(record["task_id"]) / f"{record['job_id']}.pending-event.json"
            if pending.exists():
                try:
                    recovered = self._finish_pending_locked(pending, record["task_id"], record["job_id"])
                    if recovered.get("event_history_warning"):
                        record["event_history_warning"] = recovered["event_history_warning"]
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    recovered = self._quarantine_pending_locked(record["task_id"], record["job_id"], pending, error)
                    if recovered and recovered.get("event_history_warning"):
                        record["event_history_warning"] = recovered["event_history_warning"]
            history, repairs = self._read_event_history_locked(record["task_id"], record["job_id"])
            path = self._record_path(record["task_id"], record["job_id"])
            if path.exists():
                persisted = json.loads(path.read_text(encoding="utf-8"))
                record["last_seq"] = max(record.get("last_seq", 0), persisted.get("last_seq", 0))
                if persisted.get("event_history_warning") and not record.get("event_history_warning"):
                    record["event_history_warning"] = persisted["event_history_warning"]
            record["last_seq"] = max(record.get("last_seq", 0), max((item["seq"] for item in history), default=0))
            if repairs:
                record["event_history_warning"] = self._history_warning(
                    record,
                    code="job_event_history_repaired",
                    message="事件历史已自动修复；业务状态可继续使用",
                    recovered=True,
                    details={"history_repairs": len(repairs)},
                )
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
            self.store.atomic_json(pending, {"record": record, "event": event})
            self._finish_pending_locked(pending, record["task_id"], record["job_id"])
            return event

    def _publish_event(self, record: dict[str, Any], event_type: str, **values: Any) -> dict[str, Any] | None:
        """Persist an event, but keep the business job alive if only history fails."""
        try:
            return self._append_event(record, event_type, **values)
        except EVENT_PERSISTENCE_ERRORS as error:
            log = logging.warning if record.get("event_history_warning") else logging.exception
            log(
                "Job event history degraded; persisting business state without the event",
                extra={"job_id": record.get("job_id"), "task_id": record.get("task_id")},
            )
            record["event_history_warning"] = self._history_warning(
                record,
                code="job_event_history_degraded",
                message="事件历史写入异常；业务执行未中断",
                recovered=False,
                details={"exception_type": type(error).__name__},
            )
            self._write(record)
            return None

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
                job_id = pending.name.removesuffix(".pending-event.json")
                try:
                    with self._event_lock(task_path.name, job_id):
                        try:
                            self._finish_pending_locked(pending, task_path.name, job_id)
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                            self._quarantine_pending_locked(task_path.name, job_id, pending, error)
                except EVENT_PERSISTENCE_ERRORS:
                    logging.exception("Job WAL recovery failed", extra={"pending": str(pending)})
            for path in jobs.glob("job_*.json"):
                if path.name.endswith(".pending-event.json"):
                    continue
                try:
                    record = self._read(task_path.name, path.stem)
                    self._repair_event_history(task_path.name, path.stem)
                    record = self._read(task_path.name, path.stem)
                except EVENT_PERSISTENCE_ERRORS:
                    continue
                if record.get("status") in {"running", "cancellation_requested"}:
                    record.update(status="interrupted", current_step="recovery_required", finished_at=_now())
                    record["error"] = {
                        "code": "job_interrupted",
                        "message": "服务重启时执行结果未知，未自动重试",
                    }
                    with self._guard:
                        self._publish_event(record, "interrupted", message=record["error"]["message"])
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
            "inspection.fix": {"issue_id", "rationale"},
            "delivery.publish": set(),
        }[operation]
        required = ({"prompt"} if operation in {"samples.modify", "deck.modify"} else {"issue_id"} if operation=="inspection.fix" else set())
        if set(payload) - allowed or required - set(payload):
            raise ValidationError("Job payload 字段无效")

    def create(self, task_id: str, operation: str, payload: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], bool]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValidationError("idempotency_key 无效")
        self._validate_payload(operation, payload)
        fingerprint = _fingerprint(operation, payload)
        with self._guard, self.store.lock(task_id):
            if not self._recovery_complete:
                raise ConflictError("后台任务恢复中，请稍后重试")
            state = self.service.get(task_id)
            records = self._records(task_id)
            previous = next((item for item in records if item["idempotency_key"] == idempotency_key), None)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ConflictError("相同 idempotency_key 对应了不同请求")
                return self.public(previous), False
            if operation != "delivery.publish":
                self.service.require_runtime_ready()
            if state["status"] in {"paused", "cancelled", "failed", "completed"}:
                raise ConflictError("当前任务状态不能启动长任务")
            if state["stage"] not in OPERATION_STAGES[operation]:
                raise ConflictError("当前任务阶段不能执行该长任务")
            active = next((item for item in records if item["status"] in ACTIVE), None)
            if active:
                raise ActiveJobConflict(active["job_id"])
            job_id = f"job_{uuid.uuid4().hex}"
            branch=self.store.branch_context(task_id)
            try: parent_hash=self.service.status_summary(task_id).get("latest_artifacts",{}).get("deck")
            except Exception: parent_hash=None
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
                "deadline_seconds": self.operation_budgets[operation],
                "deadline_at": None,
                "metrics": None,
                "branch_id":branch["branch_id"],
                "head_revision":state.get("revision",branch.get("head_revision")),
                "parent_hash":parent_hash,
            }
            self._write(record)
            self._publish_event(record, "queued", message="任务已进入执行队列")
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
        record: dict[str, Any] = {"task_id": task_id, "job_id": job_id, "operation": None}
        try:
            with self._guard:
                record = self._read(task_id, job_id)
                if record["status"] != "queued":
                    return
                if record["cancellation_requested"]:
                    record.update(status="cancelled", current_step="cancelled", finished_at=_now())
                    self._publish_event(record, "cancelled", message="任务已取消")
                    return
                started = datetime.now(timezone.utc)
                record.update(status="running", progress=None, current_step="domain_operation", started_at=started.isoformat(), deadline_at=(started + timedelta(seconds=record.get("deadline_seconds", self.operation_budgets[record["operation"]]))).isoformat())
                self._publish_event(record, "started", message="业务操作已开始")
            deadline = time.monotonic() + record.get("deadline_seconds", self.operation_budgets[record["operation"]])
            def publish_progress(step, message, details=None):
                with self._guard:
                    current = self._read(task_id, job_id)
                    if current["status"] not in ACTIVE:
                        return
                    current["current_step"] = step
                    metrics = {
                        key: value for key, value in (details or {}).items()
                        if key in METRIC_KEYS and isinstance(value, (str, int, float, bool))
                    }
                    if metrics:
                        current["metrics"] = metrics
                    self._publish_event(current, "checkpoint", message=message or step, metrics=current.get("metrics"))
            expected_head={"revision":record.get("head_revision")}
            branch=self.store.branch_context(task_id)
            if branch["branch_id"]!=record.get("branch_id") or branch["head_revision"]!=expected_head["revision"]:
                raise ConflictError("Job 创建后分支或分支头已变化，请在当前分支重新提交")
            def publication_guard():
                current=self.store.branch_context(task_id)
                if current["branch_id"]!=record.get("branch_id") or current["head_revision"]!=expected_head["revision"]:
                    raise ConflictError("Job 所属分支或分支头已变化，拒绝写入过期结果")
            def publication_advance(state): expected_head["revision"]=state.get("revision",expected_head["revision"])
            with execution_scope(lambda: self._read(task_id, job_id).get("cancellation_requested", False), deadline, publish_progress, publication_guard, publication_advance):
                # Readiness can change after enqueue or while a queued job is being
                # recovered. Never cross the model boundary without a fresh gate.
                try:
                    if record["operation"] != "delivery.publish":
                        self.service.require_runtime_ready()
                except RuntimeUnavailableError as error:
                    if record["operation"] == "clarification.generate":
                        self.service.wait_clarification_for_runtime(task_id, error)
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
                self._publish_event(record, "succeeded", message="业务操作已完成")
        except Exception as error:
            if isinstance(error, GatewayError):
                self.service.record_runtime_failure(error)
                if isinstance(error,GatewayUnknownResult):
                    self.schedule_runtime_recovery()
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
                        self._publish_event(record, "cancelled", message="任务已取消，未提交业务结果")
                        return
                    public = error.public()["error"] if hasattr(error, "public") else {
                        "code": "internal_error",
                        "message": "后台任务执行失败",
                        "diagnostic_id": uuid.uuid4().hex,
                    }
                    if isinstance(error, EVENT_PERSISTENCE_ERRORS):
                        public = {"code":"job_persistence_error","message":"Job 持久化失败，请重试","errno":getattr(error, "errno", None)}
                    if not hasattr(error, "public"):
                        logging.exception(
                            "background Job failed",
                            extra={"job_id": job_id, "task_id": task_id, "diagnostic_id": public.get("diagnostic_id")},
                        )
                    record.update(
                        status="failed",
                        current_step="failed",
                        finished_at=_now(),
                        error=public,
                    )
                    self._publish_event(record, "failed", message=public["message"], error=public)
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
            rounds=payload.get("max_rounds")
            if rounds is None and hasattr(self.service,"default_inspection_rounds"): rounds=self.service.default_inspection_rounds()
            return self.service.run_inspection(task_id, 2 if rounds is None else rounds, payload.get("affected_slide_ids"))
        if operation == "inspection.fix":
            return self.service.dispose_issue(task_id, payload["issue_id"], "agent_fix", payload.get("rationale", ""), "user")
        if operation == "delivery.publish":
            return self.service.publish_delivery(task_id)
        raise ValidationError("不支持的 Job 操作")

    @staticmethod
    def public(record: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "job_id", "task_id", "operation", "status", "progress", "current_step", "last_seq",
            "created_at", "started_at", "finished_at", "result", "error", "cancellation_requested",
            "deadline_seconds", "deadline_at", "metrics", "event_history_warning",
            "branch_id", "head_revision", "parent_hash",
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

    def list_page(self,task_id,status=None,operation=None,limit=25,before=None):
        records=self.list(task_id,status)
        if operation: records=[item for item in records if item["operation"]==operation]
        records=sorted(records,key=lambda item:(item["created_at"],item["job_id"]),reverse=True)
        if before:
            index=next((idx for idx,item in enumerate(records) if item["job_id"]==before),None)
            if index is None: raise ValidationError("Job 分页游标无效")
            records=records[index+1:]
        page=records[:limit]
        return {"jobs":page,"next_cursor":page[-1]["job_id"] if len(records)>limit else None}

    def create_branch(self,task_id,branch_id,source_branch=None,source_revision=None,switch=True):
        """Serialize branch changes with Job admission for the same task."""
        with self._guard, self.store.lock(task_id):
            if any(item["status"] in ACTIVE for item in self._records(task_id)):
                raise ConflictError("存在活动 Job 时不能创建并切换分支")
            return self.store.branch_from(task_id,branch_id,source_branch,source_revision,switch)

    def switch_branch(self,task_id,branch_id):
        """Prevent a Job from being admitted between the active check and switch."""
        with self._guard, self.store.lock(task_id):
            if any(item["status"] in ACTIVE for item in self._records(task_id)):
                raise ConflictError("存在活动 Job 时不能切换分支")
            return self.store.switch_branch(task_id,branch_id)

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
                self._publish_event(record, "cancelled", message="排队任务已取消")
            else:
                record.update(status="cancellation_requested", current_step="cancellation_requested")
                self._publish_event(record, "checkpoint", message="已请求取消；正在等待安全停止点")
            return self.public(record)

    def events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        snapshot = self.get(job_id)
        events = self._repair_event_history(snapshot["task_id"], job_id)
        return [event for event in events if event["seq"] > after]

    def heartbeat(self, job_id: str) -> dict[str, Any] | None:
        snapshot = self.get(job_id)
        if snapshot["status"] in TERMINAL:
            return None
        with self._guard:
            record = self._read(snapshot["task_id"], job_id)
            return self._publish_event(record, "heartbeat", message="连接正常")

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
