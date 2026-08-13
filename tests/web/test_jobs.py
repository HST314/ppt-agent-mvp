import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ppt_agent.errors import ConflictError
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


class JobServiceTests(unittest.TestCase):
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
            while time.monotonic() < deadline and jobs.get(first["job_id"])["status"] not in {"succeeded", "failed"}:
                time.sleep(0.02)
            self.assertEqual(jobs.get(first["job_id"])["status"], "succeeded")
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


if __name__ == "__main__":
    unittest.main()
