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
from ppt_agent.gateways import AgentGateway
from ppt_agent.model_clients import ModelToolCall, ModelTurn, OpenAIResponsesClient
from ppt_agent.service import TaskService
from ppt_agent.skill_runtime import SkillRuntime
from ppt_agent.store import WorkspaceStore
from ppt_agent.web.jobs import JobService
from tests.web.test_jobs import DeferredExecutor


class Client:
    def __init__(self, turns): self.turns = list(turns)
    def create(self, **_kwargs): return self.turns.pop(0)


class BlockingSDK:
    def __init__(self):
        self.responses = self
        self.started, self.closed = threading.Event(), threading.Event()
        self.seen = None
    def create(self, **kwargs):
        self.seen = kwargs; self.started.set(); self.closed.wait(2)
        raise OSError("request transport closed")
    def close(self): self.closed.set()


def adapter(sdk):
    config = type("Config", (), {"model":"m", "api_key":"k", "base_url":"https://example.com", "timeout_seconds":5})()
    return OpenAIResponsesClient(config, sdk_client=sdk)


class P0FaultInjectionGate(unittest.TestCase):
    def test_real_adapter_passes_budget_and_cancel_leaves_no_execution_unit(self):
        sdk, cancelled = BlockingSDK(), threading.Event()
        def cancel():
            self.assertTrue(sdk.started.wait(1)); cancelled.set()
        timer = threading.Timer(.03, cancel); timer.start()
        with self.assertRaises(ExecutionCancelled):
            with execution_scope(cancelled.is_set, time.monotonic() + 1):
                AgentRuntime(adapter(sdk), SkillRuntime.builtin()).run("narrative", {})
        timer.join()
        self.assertGreater(sdk.seen["timeout"], 0)
        self.assertLessEqual(sdk.seen["timeout"], 1)
        self.assertFalse(any(t.name in {"ppt-interruptible-call", "ppt-model-cancellation"} for t in threading.enumerate()))

    def test_real_adapter_clarification_cancel_recovers_retry_state(self):
        with tempfile.TemporaryDirectory() as root:
            sdk = BlockingSDK()
            service = TaskService(WorkspaceStore(root), clarifier=AgentGateway(adapter(sdk),skill=SkillRuntime.builtin()))
            # This gate exercises cancellation after dispatch. Capability-probe
            # readiness is covered separately and would consume the blocking SDK.
            service.require_clarification_runtime_ready = Mock(return_value=None)
            service.clarification_runtime_health = Mock(return_value={"status":"ready","ready":True})
            service.create("clarify-cancel")
            service.import_input("clarify-cancel", {"goal":"g", "audience":"a", "topic":"t"}, "json")
            jobs = JobService(service)
            created, _ = jobs.create("clarify-cancel", "clarification.generate", {}, "cancel-key")
            self.assertTrue(sdk.started.wait(1))
            jobs.cancel(created["job_id"])
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and jobs.get(created["job_id"])["status"] not in {"cancelled", "failed"}:
                time.sleep(.01)
            self.assertEqual(jobs.get(created["job_id"])["status"], "cancelled")
            state = service.get("clarify-cancel")
            self.assertEqual(state["waiting_reason"], "clarification_failed")
            self.assertEqual(state["required_action"], "retry_clarification")
            self.assertFalse(any(t.name in {"ppt-interruptible-call", "ppt-model-cancellation"} for t in threading.enumerate()))
            jobs.close()

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
        calls = [ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "c1")]
        client = Client([ModelTurn(None, "r1", calls), ModelTurn('{"markdown":"ok"}', "r2")])
        seen = []
        with execution_scope(lambda: False, time.monotonic() + 1, lambda step, message, details: seen.append((step, details))):
            AgentRuntime(client, SkillRuntime.builtin()).run("narrative", {})
        steps = [step for step, _details in seen]
        self.assertIn("waiting_model", steps)
        self.assertIn("provider_response", steps)
        self.assertIn("skill_loading", steps)
        self.assertIn("skill_completed", steps)
        metrics = next(details for step, details in seen if step == "skill_completed")
        self.assertEqual(metrics["tool_calls"], 1)
        self.assertEqual(metrics["max_provider_calls"], 8)

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
