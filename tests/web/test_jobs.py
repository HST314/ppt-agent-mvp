import json
import errno
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ppt_agent.errors import ConflictError, GatewayError
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
    def test_get_ignores_transaction_snapshot_seen_before_live_task(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("snapshot-safe")
            service.command("snapshot-safe", "to-clarification", "advance")
            service.command("snapshot-safe", "to-narrative", "advance")
            jobs = JobService(service, executor=DeferredExecutor())
            created, _ = jobs.create("snapshot-safe", "narrative.generate", {}, "snapshot-key")

            live_task = Path(root) / "snapshot-safe"
            snapshot = Path(root) / ".snapshot-safe.test.transaction"
            (snapshot / "jobs").mkdir(parents=True)
            (snapshot / "checkpoint.json").write_text(
                (live_task / "checkpoint.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            snapshot_job = snapshot / "jobs" / f"{created['job_id']}.json"
            snapshot_job.write_text(
                (live_task / "jobs" / snapshot_job.name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            with patch.object(Path, "iterdir", return_value=iter((snapshot, live_task))):
                found = jobs.get(created["job_id"])

            self.assertEqual(found["task_id"], "snapshot-safe")
            jobs.close()

    def test_runtime_status_uses_on_demand_policy_without_provider_calls(self):
        class DirectGateway:
            model="direct-model"
            calls=0
            def set_audit_sink(self,_sink): pass
            def probe_capabilities(self):
                self.calls += 1
                raise AssertionError("status reads must not contact the provider")

        with tempfile.TemporaryDirectory() as root:
            gateway=DirectGateway()
            service=TaskService(WorkspaceStore(root),generator=gateway)
            health=service.initialize_runtime()

        self.assertTrue(health["ready"])
        self.assertEqual(health["status"],"on_demand")
        self.assertEqual(gateway.calls,0)

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

    def test_queued_job_invokes_domain_operation_directly(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            service.create("direct")
            service.command("direct", "to-clarification", "advance")
            service.command("direct", "to-narrative", "advance")
            executor = DeferredExecutor()
            jobs = JobService(service, executor=executor)
            created, _ = jobs.create("direct", "narrative.generate", {}, "direct-key")
            with patch.object(service, "generate_narrative", return_value={"accepted":True}) as generate:
                function, args = executor.calls.pop(0)
                function(*args)
            generate.assert_called_once_with("direct",None,"all")
            self.assertEqual(jobs.get(created["job_id"])["status"], "succeeded")
            jobs.close()

    def test_recovered_queued_job_resumes_after_local_recovery(self):
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

    def test_clarification_failure_is_task_scoped_and_retry_calls_model_again(self):
        class Clarifier:
            model="clarifier"
            calls=0
            def clarify(self,_payload):
                self.calls += 1
                if self.calls == 1:
                    raise GatewayError("认证失败",code="model_authentication_failed")
                return {"questions":[],"model":self.model}
        with tempfile.TemporaryDirectory() as root:
            clarifier=Clarifier()
            service = TaskService(WorkspaceStore(root),clarifier=clarifier)
            service.create("clarification-retry")
            service.import_input("clarification-retry", {"topic": "新品"})
            executor = DeferredExecutor()
            jobs = JobService(service, executor=executor)
            created, _ = jobs.create(
                "clarification-retry",
                "clarification.generate",
                {},
                "clarification-first-key",
            )
            function, args = executor.calls.pop(0)
            function(*args)
            clarification = service.input_view("clarification-retry")["clarification"]
            self.assertEqual(clarification["status"], "failed")
            self.assertEqual(clarification["error"]["code"], "model_authentication_failed")
            state=service.get("clarification-retry")
            self.assertEqual(state["waiting_reason"],"clarification_failed")
            self.assertEqual(state["required_action"],"retry_clarification")
            self.assertEqual(jobs.get(created["job_id"])["status"], "failed")
            retry,_=jobs.create("clarification-retry","clarification.generate",{},"clarification-retry-key")
            function,args=executor.calls.pop(0)
            function(*args)
            self.assertEqual(jobs.get(retry["job_id"])["status"],"succeeded")
            self.assertEqual(clarifier.calls,2)
            self.assertTrue(service.input_view("clarification-retry")["clarification"]["confirmed"])
            jobs.close()

    def test_failed_generation_keeps_runtime_available_for_user_retry(self):
        class RetryService(TaskService):
            calls=0
            def generate_narrative(self,task_id,prompt=None,scope="all"):
                self.calls += 1
                if self.calls == 1:
                    raise GatewayError("上游暂时不可用",code="model_upstream_unavailable",retryable=True)
                return {"accepted":True}

        with tempfile.TemporaryDirectory() as root:
            service=RetryService(WorkspaceStore(root))
            service.create("retry")
            service.command("retry","to-clarification","advance")
            service.command("retry","to-narrative","advance")
            executor=DeferredExecutor()
            jobs=JobService(service,executor=executor)
            first,_=jobs.create("retry","narrative.generate",{},"retry-first")
            function,args=executor.calls.pop(0)
            function(*args)
            self.assertEqual(jobs.get(first["job_id"])["status"],"failed")
            self.assertEqual(jobs.get(first["job_id"])["error"]["code"],"model_upstream_unavailable")
            self.assertTrue(service.runtime_health()["ready"])
            second,_=jobs.create("retry","narrative.generate",{},"retry-second")
            function,args=executor.calls.pop(0)
            function(*args)
            self.assertEqual(jobs.get(second["job_id"])["status"],"succeeded")
            self.assertEqual(service.calls,2)
            jobs.close()

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
