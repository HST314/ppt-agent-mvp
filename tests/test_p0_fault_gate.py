import errno
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ppt_agent.agent_runtime import AgentRuntime
from ppt_agent.execution import ExecutionCancelled, ExecutionDeadlineExceeded, execution_scope, interruptible
from ppt_agent.model_clients import ModelToolCall, ModelTurn
from ppt_agent.service import TaskService
from ppt_agent.skill_runtime import SkillRuntime
from ppt_agent.store import WorkspaceStore
from ppt_agent.web.jobs import JobService
from tests.web.test_jobs import DeferredExecutor


class Client:
    def __init__(self, turns): self.turns = list(turns)
    def create(self, **_kwargs): return self.turns.pop(0)


class P0FaultInjectionGate(unittest.TestCase):
    def test_blocking_call_is_cancelled_within_sla(self):
        cancelled, release = threading.Event(), threading.Event()
        started = time.monotonic()
        with self.assertRaises(ExecutionCancelled):
            with execution_scope(cancelled.is_set, time.monotonic() + 5):
                threading.Timer(.05, cancelled.set).start()
                interruptible(lambda: release.wait(2), poll_seconds=.005)
        release.set()
        self.assertLess(time.monotonic() - started, .25)

    def test_blocking_call_obeys_absolute_deadline(self):
        release = threading.Event(); started = time.monotonic()
        with self.assertRaises(ExecutionDeadlineExceeded):
            with execution_scope(lambda: False, time.monotonic() + .05):
                interruptible(lambda: release.wait(2), poll_seconds=.005)
        release.set()
        self.assertLess(time.monotonic() - started, .25)

    def test_task_scoped_commit_rejects_cancelled_publication(self):
        with tempfile.TemporaryDirectory() as root:
            store = WorkspaceStore(root); store.create("task", {"revision": 0})
            event = {"event_id": "e1"}
            with self.assertRaises(ExecutionCancelled):
                with execution_scope(lambda: True, time.monotonic() + 1):
                    store.commit("task", {"revision": 1}, event)
            self.assertEqual(store.checkpoint("task")["revision"], 0)
            self.assertEqual((Path(root) / "task" / "events.jsonl").read_text(), "")

    def test_job_wal_recovers_after_record_replace_retry_exhaustion(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root)); service.create("wal")
            service.command("wal", "to-clarification", "advance")
            jobs = JobService(service, executor=DeferredExecutor())
            created, _ = jobs.create("wal", "clarification.generate", {}, "key")
            job_id = created["job_id"]; record = jobs._read("wal", job_id)
            real_replace, failures = os.replace, 0
            def fail_record(source, destination):
                nonlocal failures
                if str(destination).endswith(f"{job_id}.json") and failures < 5:
                    failures += 1; raise PermissionError(errno.EACCES, "injected busy")
                return real_replace(source, destination)
            with patch("ppt_agent.store.os.replace", side_effect=fail_record):
                with self.assertRaises(PermissionError):
                    jobs._append_event(record, "checkpoint", message="fault")
            recovered = jobs.get(job_id)
            self.assertEqual(recovered["last_seq"], jobs.events(job_id)[-1]["seq"])
            self.assertFalse((Path(root)/"wal"/"jobs"/f"{job_id}.pending-event.json").exists())
            jobs.close()

    def test_real_business_checkpoints_are_emitted(self):
        calls = [ModelToolCall("list_skill_files", "{}", "c1")]
        client = Client([ModelTurn(None, "r1", calls), ModelTurn('{"markdown":"ok"}', "r2")])
        seen = []
        with execution_scope(lambda: False, time.monotonic() + 1, lambda step, message: seen.append(step)):
            AgentRuntime(client, SkillRuntime.builtin()).run("narrative", {})
        self.assertIn("waiting_model", seen)
        self.assertIn("skill_loading", seen)

    def test_clarification_infrastructure_failure_is_normalized(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root))
            view = {"state": {"waiting_reason": "wait_for_clarification"}, "clarification": {"status": "generating"}}
            service.input_view = Mock(return_value=view)
            service._record_clarification = Mock(return_value={"status": "failed"})
            result = service.recover_clarification_failure("task", ExecutionDeadlineExceeded())
            self.assertEqual(result["status"], "failed")
            args = service._record_clarification.call_args.args
            self.assertEqual(args[2], [])
            self.assertEqual(args[3], "failed")
            self.assertEqual(args[5]["code"], "stage_deadline_exceeded")


if __name__ == "__main__": unittest.main()
