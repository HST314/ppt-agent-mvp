import json
import errno
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ppt_agent.errors import ConflictError, GatewayError, RuntimeUnavailableError
from ppt_agent.execution import ExecutionDeadlineExceeded, progress
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web.jobs import JobService


class DeferredExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, function, *args):
        self.calls.append((function, args))

    def shutdown(self, **_kwargs):
        pass


class BlockingService(TaskService):
    def __init__(self, store):
        super().__init__(store)
        self.started = threading.Event()
        self.release = threading.Event()

    def generate_narrative(self, task_id, prompt=None, scope="all"):
        self.started.set()
        self.release.wait(2)
        return {"accepted": True}


class DeadlineService(TaskService):
    def generate_narrative(self, task_id, prompt=None, scope="all"):
        self.failure = ExecutionDeadlineExceeded()
        raise self.failure


class ProgressService(TaskService):
    def generate_narrative(self, task_id, prompt=None, scope="all"):
        progress("waiting_model", "等待模型响应")
        return {"accepted": True}


class JobServiceTests(unittest.TestCase):
    def test_stage_deadline_keeps_diagnostic_id_and_specific_error_code(self):
        with tempfile.TemporaryDirectory() as root:
            service = DeadlineService(WorkspaceStore(root))
            service.create("deadline")
            service.command("deadline", "to-clarification", "advance")
            service.command("deadline", "to-narrative", "advance")
            executor = DeferredExecutor()
            jobs = JobService(service, executor=executor)
            created, _ = jobs.create("deadline", "narrative.generate", {}, "deadline-key")

            function, args = executor.calls.pop(0)
            function(*args)

            error = jobs.get(created["job_id"])["error"]
            self.assertEqual(error["code"], "stage_deadline_exceeded")
            self.assertEqual(error["diagnostic_id"], service.failure.diagnostic_id)
            jobs.close()

    def test_agent_runtime_starts_closed_before_capability_probe(self):
        class ModelGateway:
            model = "model"
            def set_audit_sink(self, _sink): pass
            def probe_capabilities(self): return {"strict_json_schema": True}
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), generator=ModelGateway())
            self.assertEqual(service.runtime_health()["status"], "not_checked")
            self.assertFalse(service.runtime_health()["ready"])
            service.initialize_runtime()
            self.assertTrue(service.runtime_health()["ready"])

    def test_runtime_is_rechecked_before_a_queued_job_crosses_model_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("gated")
            service.command("gated", "to-clarification", "advance")
            service.command("gated", "to-narrative", "advance")
            executor = DeferredExecutor()
            jobs = JobService(service, executor=executor)
            created, _ = jobs.create("gated", "narrative.generate", {}, "gate-key")
            service.record_runtime_failure(GatewayError("auth", code="model_authentication_failed"))
            with patch.object(service, "generate_narrative", wraps=service.generate_narrative) as generate:
                function, args = executor.calls.pop(0)
                function(*args)
            self.assertFalse(generate.called)
            failed = jobs.get(created["job_id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"]["code"], "runtime_unavailable")
            self.assertEqual(failed["error"]["runtime_error_code"], "model_authentication_failed")
            jobs.close()

    def test_recovered_queued_job_can_wait_for_startup_readiness_probe(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("deferred")
            service.command("deferred", "to-clarification", "advance")
            service.command("deferred", "to-narrative", "advance")
            first = JobService(service, executor=DeferredExecutor())
            created, _ = first.create("deferred", "narrative.generate", {}, "deferred-key")
            first.close()

            executor = DeferredExecutor()
            recovered = JobService(service, executor=executor, defer_queued_recovery=True)
            self.assertEqual(recovered.get(created["job_id"])["status"], "queued")
            self.assertEqual(executor.calls, [])
            recovered.resume_recovered_queued()
            self.assertEqual(len(executor.calls), 1)
            recovered.close()

    def test_queued_clarification_closes_when_runtime_degrades_before_execution(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("clarification-gated")
            service.import_input("clarification-gated", {"topic": "新品"})
            executor = DeferredExecutor()
            jobs = JobService(service, executor=executor)
            created, _ = jobs.create(
                "clarification-gated",
                "clarification.generate",
                {},
                "clarification-gate-key",
            )
            failure = GatewayError("auth", code="model_authentication_failed")
            service.record_runtime_failure(failure)
            function, args = executor.calls.pop(0)
            function(*args)
            clarification = service.input_view("clarification-gated")["clarification"]
            self.assertEqual(clarification["status"], "failed")
            self.assertEqual(clarification["error"]["code"], "runtime_unavailable")
            # 落入本任务记录的运行时错误换发本任务自己的诊断 ID，且不引用
            # 其他任务的 Agent 审计，避免跨任务错误污染。
            self.assertNotEqual(clarification["error"]["diagnostic_id"], failure.diagnostic_id)
            self.assertNotIn("agent_audit_id", clarification["error"])
            self.assertEqual(jobs.get(created["job_id"])["status"], "failed")
            jobs.close()

    def test_only_transport_auth_and_upstream_failures_degrade_global_runtime(self):
        behavior_codes = ("gateway_error", "probe_tool_final_invalid_output")
        degrading_codes = (
            "model_timeout",
            "model_connection_error",
            "model_authentication_failed",
            "model_permission_denied",
            "model_upstream_unavailable",
        )
        for code in behavior_codes:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                service = TaskService(WorkspaceStore(root))
                service.record_runtime_failure(GatewayError("模型行为失败", code=code))
                self.assertTrue(service.runtime_health()["ready"])
        for code in degrading_codes:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                service = TaskService(WorkspaceStore(root))
                service.record_runtime_failure(GatewayError("服务降级", code=code))
                self.assertFalse(service.runtime_health()["ready"])

    def test_runtime_gate_reissues_diagnostic_and_drops_foreign_audit(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            failure = GatewayError("上游不可用", code="model_upstream_unavailable")
            failure.agent_audit_id = "agent-audit-from-another-task"
            service.record_runtime_failure(failure)
            with self.assertRaises(RuntimeUnavailableError) as caught:
                service.require_runtime_ready()
            error = caught.exception.public()["error"]
            self.assertEqual(error["runtime_error_code"], "model_upstream_unavailable")
            self.assertNotEqual(error["diagnostic_id"], failure.diagnostic_id)
            self.assertNotIn("agent_audit_id", error)

    def test_probe_originated_runtime_error_keeps_probe_audit_reference(self):
        class FailingProbeGateway:
            model = "probe-model"
            def set_audit_sink(self, _sink): pass
            def probe_capabilities(self, probe_id=None):
                error = GatewayError("认证失败", code="model_authentication_failed")
                error.agent_audit_id = "agent-audit-probe"
                raise error
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), generator=FailingProbeGateway())
            service.initialize_runtime()
            with self.assertRaises(RuntimeUnavailableError) as caught:
                service.require_runtime_ready()
            error = caught.exception.public()["error"]
            self.assertEqual(error["agent_audit_id"], "agent-audit-probe")
            self.assertTrue(error["probe_id"])

    def test_atomic_json_retries_transient_replace_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as root:
            store = WorkspaceStore(root)
            target = Path(root) / "value.json"
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError(errno.EACCES, "busy")
                return real_replace(source, destination)

            with patch("ppt_agent.store.os.replace", side_effect=flaky_replace):
                store.atomic_json(target, {"ok": True})
            self.assertEqual(json.loads(target.read_text()), {"ok": True})
            self.assertEqual(list(Path(root).glob(".*.tmp")), [])

    def test_persisted_chinese_job_data_is_always_read_as_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("chinese")
            service.command("chinese", "to-clarification", "advance")
            service.command("chinese", "to-narrative", "advance")
            jobs = JobService(service, executor=DeferredExecutor())
            created, _ = jobs.create("chinese", "narrative.generate", {"prompt": "生成中文叙事"}, "cn-key")
            original = Path.read_text

            def windows_read_text(path, *args, **kwargs):
                if not args and kwargs.get("encoding") is None:
                    raise UnicodeDecodeError("gbk", b"\xaa", 0, 1, "模拟 Windows 默认编码")
                return original(path, *args, **kwargs)

            with patch.object(Path, "read_text", windows_read_text):
                self.assertEqual(jobs.get(created["job_id"])["status"], "queued")
                self.assertEqual(jobs.events(created["job_id"])[0]["type"], "queued")
            jobs.close()

    def test_queued_cancel_is_terminal_without_invocation(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("queued")
            service.command("queued", "to-clarification", "advance")
            service.command("queued", "to-narrative", "advance")
            executor = DeferredExecutor()
            jobs = JobService(service, executor=executor)
            created, is_new = jobs.create("queued", "narrative.generate", {}, "key-1")
            self.assertTrue(is_new)
            self.assertEqual(created["status"], "queued")
            cancelled = jobs.cancel(created["job_id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual([item["type"] for item in jobs.events(created["job_id"])], ["queued", "cancelled"])
            jobs.close()

    def test_latest_job_per_operation_supports_persistent_failure_feedback(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("latest")
            service.command("latest", "to-clarification", "advance")
            service.command("latest", "to-narrative", "advance")
            jobs = JobService(service, executor=DeferredExecutor())
            first, _ = jobs.create("latest", "narrative.generate", {}, "first")
            jobs.cancel(first["job_id"])
            second, _ = jobs.create("latest", "narrative.generate", {"prompt": "retry"}, "second")

            latest = jobs.latest_by_operation("latest")

            self.assertEqual(len(latest), 1)
            self.assertEqual(latest[0]["job_id"], second["job_id"])
            self.assertEqual(latest[0]["status"], "queued")
            jobs.close()

    def test_task_job_mutual_exclusion(self):
        with tempfile.TemporaryDirectory() as root:
            service = BlockingService(WorkspaceStore(root))
            service.create("mutual")
            service.command("mutual", "to-clarification", "advance")
            service.command("mutual", "to-narrative", "advance")
            jobs = JobService(service)
            first, _ = jobs.create("mutual", "narrative.generate", {}, "first")
            self.assertTrue(service.started.wait(1))
            with self.assertRaises(ConflictError):
                jobs.create("mutual", "narrative.generate", {"prompt": "second"}, "second")
            requested = jobs.cancel(first["job_id"])
            self.assertEqual(requested["status"], "cancellation_requested")
            service.release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and jobs.get(first["job_id"])["status"] not in {"succeeded", "failed", "cancelled"}:
                time.sleep(0.02)
            self.assertEqual(jobs.get(first["job_id"])["status"], "cancelled")
            self.assertEqual(jobs.events(first["job_id"])[-1]["type"], "cancelled")
            jobs.close()

    def test_running_job_becomes_interrupted_on_restart(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("restart")
            jobs_dir = Path(root) / "restart" / "jobs"
            jobs_dir.mkdir()
            record = {
                "job_id": "job_restart",
                "task_id": "restart",
                "operation": "narrative.generate",
                "payload": {},
                "idempotency_key": "restart-key",
                "fingerprint": "x",
                "status": "running",
                "progress": None,
                "current_step": "domain_operation",
                "last_seq": 0,
                "created_at": "2026-08-13T00:00:00+00:00",
                "started_at": "2026-08-13T00:00:01+00:00",
                "finished_at": None,
                "result": None,
                "error": None,
                "cancellation_requested": False,
            }
            (jobs_dir / "job_restart.json").write_text(json.dumps(record))
            jobs = JobService(service, executor=DeferredExecutor())
            snapshot = jobs.get("job_restart")
            self.assertEqual(snapshot["status"], "interrupted")
            self.assertEqual(snapshot["error"]["code"], "job_interrupted")
            self.assertEqual(jobs.events("job_restart")[-1]["type"], "interrupted")
            jobs.close()

    def test_pending_event_is_completed_before_restart_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("pending")
            service.command("pending", "to-clarification", "advance")
            service.command("pending", "to-narrative", "advance")
            first = JobService(service, executor=DeferredExecutor())
            created, _ = first.create("pending", "narrative.generate", {}, "pending-key")
            job_id = created["job_id"]
            record_path = Path(root) / "pending" / "jobs" / f"{job_id}.json"
            record = json.loads(record_path.read_text())
            record.update(status="running", current_step="domain_operation", last_seq=2)
            event = {"seq": 2, "job_id": job_id, "type": "started", "progress": None, "step": "domain_operation", "message": "业务操作已开始", "at": "2026-08-13T00:00:01+00:00"}
            pending = record_path.with_name(f"{job_id}.pending-event.json")
            service.store.atomic_json(pending, {"record": record, "event": event})
            first.close()

            recovered = JobService(service, executor=DeferredExecutor())
            self.assertFalse(pending.exists())
            events = recovered.events(job_id)
            self.assertEqual([item["seq"] for item in events], [1, 2, 3])
            self.assertEqual([item["type"] for item in events], ["queued", "started", "interrupted"])
            self.assertEqual(recovered.get(job_id)["status"], "interrupted")
            recovered.close()

    def test_two_coordinators_allocate_unique_monotonic_event_sequences(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("multi-writer")
            service.command("multi-writer", "to-clarification", "advance")
            service.command("multi-writer", "to-narrative", "advance")
            first = JobService(service, executor=DeferredExecutor())
            created, _ = first.create("multi-writer", "narrative.generate", {}, "multi-writer-key")
            second = JobService(service, executor=DeferredExecutor())
            first_record = first._read("multi-writer", created["job_id"])
            second_record = second._read("multi-writer", created["job_id"])
            barrier = threading.Barrier(2)

            def append(coordinator, record, message):
                barrier.wait()
                coordinator._append_event(record, "checkpoint", message=message)

            left = threading.Thread(target=append, args=(first, first_record, "left"))
            right = threading.Thread(target=append, args=(second, second_record, "right"))
            left.start(); right.start(); left.join(2); right.join(2)

            events = first.events(created["job_id"])
            self.assertEqual([item["seq"] for item in events], [1, 2, 3])
            self.assertEqual(len({(item["job_id"], item["seq"]) for item in events}), 3)
            first.close(); second.close()

    def test_legacy_duplicate_history_and_conflicting_pending_are_repaired_on_restart(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("repair")
            service.command("repair", "to-clarification", "advance")
            service.command("repair", "to-narrative", "advance")
            first = JobService(service, executor=DeferredExecutor())
            created, _ = first.create("repair", "narrative.generate", {}, "repair-key")
            job_id = created["job_id"]
            jobs_dir = Path(root) / "repair" / "jobs"
            record_path = jobs_dir / f"{job_id}.json"
            event_path = jobs_dir / f"{job_id}.events.jsonl"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record.update(status="running", current_step="skill_loading", last_seq=2)
            service.store.atomic_json(record_path, record)
            started = {
                "seq": 2, "job_id": job_id, "type": "started", "progress": None,
                "step": "domain_operation", "message": "业务操作已开始", "at": "2026-08-17T09:00:00+00:00",
            }
            checkpoint = {
                "seq": 3, "job_id": job_id, "type": "checkpoint", "progress": None,
                "step": "skill_loading", "message": "读取第二个 Skill", "at": "2026-08-17T09:00:01+00:00",
            }
            with open(event_path, "a", encoding="utf-8") as stream:
                for event in (started, started, checkpoint):
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            pending_record = dict(record, last_seq=3, current_step="skill_loading")
            conflicting = {
                "seq": 3, "job_id": job_id, "type": "checkpoint", "progress": None,
                "step": "skill_loading", "message": "读取第三个 Skill", "at": "2026-08-17T09:00:02+00:00",
            }
            pending = jobs_dir / f"{job_id}.pending-event.json"
            service.store.atomic_json(pending, {"record": pending_record, "event": conflicting})
            first.close()

            recovered = JobService(service, executor=DeferredExecutor())
            snapshot = recovered.get(job_id)
            events = recovered.events(job_id)

            self.assertEqual(snapshot["status"], "interrupted")
            self.assertEqual(snapshot["event_history_warning"]["code"], "job_event_history_repaired")
            self.assertEqual([event["seq"] for event in events], [1, 2, 3, 4, 5])
            self.assertEqual(events[3]["storage_repair"]["reason"], "pending_sequence_conflict")
            self.assertEqual(events[-1]["type"], "interrupted")
            self.assertTrue((jobs_dir / f"{job_id}.events.jsonl.recovery.bak").exists())
            self.assertTrue((jobs_dir / f"{job_id}.pending-event.json.recovery.bak").exists())
            self.assertFalse(pending.exists())
            recovered.close()

    def test_corrupt_pending_is_quarantined_without_breaking_job_reads(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("corrupt-pending")
            service.command("corrupt-pending", "to-clarification", "advance")
            service.command("corrupt-pending", "to-narrative", "advance")
            jobs = JobService(service, executor=DeferredExecutor())
            created, _ = jobs.create("corrupt-pending", "narrative.generate", {}, "corrupt-key")
            job_id = created["job_id"]
            jobs_dir = Path(root) / "corrupt-pending" / "jobs"
            pending = jobs_dir / f"{job_id}.pending-event.json"
            pending.write_text("{not-json", encoding="utf-8")

            snapshot = jobs.get(job_id)

            self.assertEqual(snapshot["status"], "queued")
            self.assertEqual(snapshot["event_history_warning"]["code"], "job_event_history_degraded")
            self.assertTrue((jobs_dir / f"{job_id}.pending-event.json.corrupt.bak").exists())
            self.assertFalse(pending.exists())
            jobs.close()

    def test_checkpoint_history_failure_does_not_kill_business_job(self):
        with tempfile.TemporaryDirectory() as root:
            service = ProgressService(WorkspaceStore(root))
            service.create("history-warning")
            service.command("history-warning", "to-clarification", "advance")
            service.command("history-warning", "to-narrative", "advance")
            executor = DeferredExecutor()
            jobs = JobService(service, executor=executor)
            created, _ = jobs.create("history-warning", "narrative.generate", {}, "history-key")
            original = jobs._append_event

            def fail_checkpoint(record, event_type, **values):
                if event_type == "checkpoint":
                    raise OSError(errno.EIO, "injected event history failure")
                return original(record, event_type, **values)

            with patch.object(jobs, "_append_event", side_effect=fail_checkpoint):
                function, args = executor.calls.pop(0)
                function(*args)

            snapshot = jobs.get(created["job_id"])
            self.assertEqual(snapshot["status"], "succeeded")
            self.assertEqual(snapshot["event_history_warning"]["code"], "job_event_history_degraded")
            self.assertEqual([event["type"] for event in jobs.events(created["job_id"])], ["queued", "started", "succeeded"])
            jobs.close()


if __name__ == "__main__":
    unittest.main()
