import json
import tempfile
import time
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ppt_agent.errors import GatewayError
from ppt_agent.gateways import AgentGateway
from ppt_agent.model_clients import OpenAIResponsesClient
from ppt_agent.service import TaskService
from ppt_agent.skill_runtime import SkillRuntime
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app
from ppt_agent.web.assets import FRONTEND_BUILD


class FastAPIAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = TaskService(WorkspaceStore(self.tmp.name))
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_health_shell_static_and_retired_legacy_routes(self):
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["web_runtime"], "fastapi")
        self.assertTrue(health.json()["runtime_ready"])
        self.assertEqual(health.json()["model_capabilities"]["status"],"not_required")
        self.assertRegex(health.json()["backend_commit"],r"^(unknown|[0-9a-fA-F]{7,40})$")
        self.assertRegex(health.json()["config_summary_sha256"],r"^[0-9a-f]{64}$")
        self.assertEqual(health.headers["x-content-type-options"], "nosniff")

        html = self.client.get("/")
        self.assertEqual(html.status_code, 200)
        self.assertIn("PPT Agent 工作台", html.text)
        self.assertIn("script-src 'self'", html.headers["content-security-policy"])
        self.assertNotIn("unsafe-inline", html.headers["content-security-policy"])
        self.assertEqual(html.headers["cache-control"], "no-store")
        self.assertEqual(html.headers["x-ppt-agent-build"], FRONTEND_BUILD)
        self.assertIn(f'data-build="{FRONTEND_BUILD}"', html.text)
        self.assertIn(f'/static/js/app.js?v={FRONTEND_BUILD}', html.text)

        current_asset = self.client.get(f"/static/js/app.js?v={FRONTEND_BUILD}")
        self.assertEqual(current_asset.status_code, 200)
        self.assertEqual(current_asset.headers["cache-control"], "public, max-age=31536000, immutable")
        stale_asset = self.client.get("/static/js/app.js?v=previous-build")
        self.assertEqual(stale_asset.status_code, 200)
        self.assertEqual(stale_asset.headers["cache-control"], "no-cache")
        self.assertEqual(self.client.get("/static/js/app.js").headers["cache-control"], "no-cache")

        created = self.client.post("/v1/tasks", json={"task_id": "shell", "mode": "manual"})
        self.assertEqual(created.status_code, 201)
        shell = self.client.get("/v1/tasks/shell/shell").json()
        self.assertEqual(len(shell["stages"]), 8)
        self.assertEqual(shell["stages"][0]["status"], "current")
        self.assertEqual(shell["stages"][1]["status"], "locked")
        self.assertIn("前置条件", shell["stages"][1]["lock_reason"])

        self.assertIn("PPT Agent 工作台", self.client.get("/tasks/shell/outline").text)
        legacy = self.client.get("/legacy/tasks/shell")
        self.assertEqual(legacy.status_code, 404)
        self.assertNotIn("unsafe-inline", legacy.headers["content-security-policy"])

    def test_failed_startup_model_probe_marks_readiness_unavailable(self):
        class ProbeFailure:
            model="probe-model"
            def set_audit_sink(self,_sink): pass
            def probe_capabilities(self): raise RuntimeError("provider details must stay private")
        with tempfile.TemporaryDirectory() as root:
            service=TaskService(WorkspaceStore(root),generator=ProbeFailure())
            with TestClient(create_app(service)) as client:
                health_response=client.get("/healthz")
                ready_response=client.get("/readyz")
                live_response=client.get("/livez")
                browser_status_response=client.get("/v1/runtime/status")
                health=health_response.json()
                probes=client.get("/v1/runtime/probes?limit=1").json()["probes"]
            self.assertEqual(health_response.status_code,503)
            self.assertEqual(ready_response.status_code,503)
            self.assertEqual(live_response.status_code,200)
            self.assertEqual(browser_status_response.status_code,200)
            self.assertFalse(browser_status_response.json()["runtime_ready"])
            self.assertEqual(live_response.json()["status"],"ok")
            self.assertFalse(health["runtime_ready"])
            self.assertEqual(health["status"],"unavailable")
            self.assertEqual(health["model_capabilities"]["error"]["code"],"capability_probe_failed")
            self.assertEqual(health["model_capabilities"]["failed_check"],"capability_contract")
            self.assertRegex(health["model_capabilities"]["probe_id"],r"^runtime-probe-[0-9a-f]{32}$")
            self.assertEqual(probes[0]["probe_id"],health["model_capabilities"]["probe_id"])
            self.assertEqual(probes[0]["events"][-1]["sdk_exception_type"],"RuntimeError")
            self.assertEqual(TaskService(WorkspaceStore(root)).runtime_probes(1)[0]["probe_id"],probes[0]["probe_id"])
            self.assertNotIn("provider details",str(health))
            self.assertNotIn("provider details",str(probes))

    def test_real_adapter_unknown_sdk_failures_map_each_probe_layer_and_persist_safely(self):
        class ProbeSDK:
            def __init__(self, fail_at):
                self.responses = self
                self.fail_at = fail_at
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                if self.calls == self.fail_at:
                    raise RuntimeError("raw provider message with secret-key")
                responses = {
                    1: SimpleNamespace(output_text="OK", id="provider-basic-id", output=[]),
                    2: SimpleNamespace(output_text='{"questions":[]}', id="provider-schema-id", output=[]),
                    3: SimpleNamespace(
                        output_text="",
                        id="provider-tool-id",
                        output=[SimpleNamespace(type="function_call", name="list_skill_files", arguments="{}", call_id="provider-call-id")],
                    ),
                    4: SimpleNamespace(output_text='{"markdown":"probe-ok"}', id="provider-final-id", output=[]),
                }
                return responses[self.calls]

        config = SimpleNamespace(
            model="probe-model",
            api_key="secret-key",
            base_url="https://provider.example/v1",
            timeout_seconds=1,
        )
        cases = {
            "basic_response": (1, "probe_basic_response_failed"),
            "strict_json_schema": (2, "probe_invalid_output"),
            "tool_round_trip": (3, "probe_tool_round_failed"),
        }
        for failed_check, (fail_at, expected_code) in cases.items():
            with self.subTest(failed_check=failed_check), tempfile.TemporaryDirectory() as root:
                adapter = OpenAIResponsesClient(config, sdk_client=ProbeSDK(fail_at))
                gateway = AgentGateway(adapter, skill=SkillRuntime.builtin(), model=config.model)
                service = TaskService(WorkspaceStore(root), generator=gateway, clarifier=gateway)
                with TestClient(create_app(service)) as client:
                    status = client.get("/v1/runtime/status").json()["model_capabilities"]
                    persisted = client.get("/v1/runtime/probes?limit=1").json()["probes"][0]
                    client.post("/v1/tasks", json={"task_id": f"probe-{fail_at}", "mode": "manual"})
                    blocked = client.post(
                        f"/v1/tasks/probe-{fail_at}/input",
                        json={"source": {"topic": "probe lineage"}},
                    ).json()["clarification"]["error"]

                probe_id = status["probe_id"]
                self.assertEqual(status["failed_check"], failed_check)
                self.assertEqual(status["error"]["code"], expected_code)
                self.assertEqual(status["error"]["probe_id"], probe_id)
                self.assertEqual(persisted["probe_id"], probe_id)
                self.assertEqual(persisted["failed_check"], failed_check)
                self.assertEqual(persisted["error"]["code"], expected_code)
                failure_event = persisted["events"][-1]
                self.assertEqual(failure_event["error_code"], expected_code)
                self.assertEqual(failure_event["category"], "sdk_error")
                self.assertEqual(failure_event["sdk_exception_type"], "RuntimeError")
                self.assertEqual(blocked["runtime_error_code"], expected_code)
                self.assertEqual(blocked["failed_check"], failed_check)
                self.assertEqual(blocked["probe_id"], probe_id)

                restarted = TaskService(WorkspaceStore(root)).runtime_probes(1)[0]
                self.assertEqual(restarted, persisted)
                serialized = json.dumps(
                    {"status": status, "persisted": persisted, "blocked": blocked, "restarted": restarted}
                )
                self.assertNotIn("raw provider message", serialized)
                self.assertNotIn("secret-key", serialized)

    def test_tool_probe_failure_modes_persist_precise_diagnostics_across_restart(self):
        class ProbeSDK:
            def __init__(self, scenario):
                self.responses = self
                self.scenario = scenario
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                common = {
                    1: SimpleNamespace(output_text="OK", id="provider-basic-id", output=[]),
                    2: SimpleNamespace(output_text='{"questions":[]}', id="provider-schema-id", output=[]),
                }
                if self.calls in common:
                    return common[self.calls]
                if self.scenario == "missing":
                    return SimpleNamespace(output_text="I will not call a tool", id="provider-no-tool-id", output=[])
                if self.calls == 3:
                    return SimpleNamespace(
                        output_text="",
                        id="provider-tool-id",
                        output=[SimpleNamespace(type="function_call", name="list_skill_files", arguments="{}", call_id="provider-call-id")],
                    )
                return SimpleNamespace(output_text="not-json", id=f"provider-invalid-{self.calls}", output=[])

        cases = {
            "missing": ("probe_tool_call_missing", "tool_request", "capability_probe_failed", 0),
            "final_invalid": ("probe_tool_final_invalid_output", "tool_final_output", "invalid_output", 1),
        }
        config = SimpleNamespace(
            model="probe-model",
            api_key="secret-key",
            base_url="https://provider.example/v1",
            timeout_seconds=1,
        )
        for scenario, (code, phase, reason, tool_calls) in cases.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as root:
                adapter = OpenAIResponsesClient(config, sdk_client=ProbeSDK(scenario))
                gateway = AgentGateway(adapter, skill=SkillRuntime.builtin(), model=config.model)
                service = TaskService(WorkspaceStore(root), generator=gateway, clarifier=gateway)
                with TestClient(create_app(service)) as client:
                    status = client.get("/v1/runtime/status").json()["model_capabilities"]
                    persisted = client.get("/v1/runtime/probes?limit=1").json()["probes"][0]
                    client.post("/v1/tasks", json={"task_id": f"probe-{scenario}", "mode": "manual"})
                    blocked = client.post(
                        f"/v1/tasks/probe-{scenario}/input",
                        json={"source": {"goal": "演示", "audience": "客户", "topic": "诊断"}},
                    ).json()["clarification"]["error"]

                for error in (status["error"], persisted["error"]):
                    self.assertEqual(error["code"], code)
                    self.assertEqual(error["probe_phase"], phase)
                    self.assertEqual(error["terminal_reason"], reason)
                    self.assertEqual(error["tool_calls"], tool_calls)
                self.assertEqual(persisted["failed_check"], "tool_round_trip")
                self.assertEqual(persisted["events"][-1]["probe_phase"], phase)
                self.assertEqual(blocked["runtime_error_code"], code)
                self.assertEqual(blocked["probe_phase"], phase)
                self.assertEqual(blocked["terminal_reason"], reason)
                self.assertEqual(blocked["tool_calls"], tool_calls)
                self.assertEqual(TaskService(WorkspaceStore(root)).runtime_probes(1)[0], persisted)

                serialized = json.dumps({"status": status, "persisted": persisted, "blocked": blocked})
                self.assertNotIn("I will not call a tool", serialized)
                self.assertNotIn("not-json", serialized)
                self.assertNotIn("secret-key", serialized)

    def test_unready_clarifier_does_not_enqueue_and_preserves_fallback(self):
        class UnreadyClarifier:
            model="unready-model"
            calls=0
            def set_audit_sink(self,_sink): pass
            def probe_capabilities(self):
                raise GatewayError(
                    "模型服务认证失败，请联系管理员检查凭据",
                    code="model_authentication_failed",
                )
            def clarify(self,_payload):
                self.calls += 1
                raise AssertionError("unready model must not be invoked")
        with tempfile.TemporaryDirectory() as root:
            clarifier=UnreadyClarifier()
            service=TaskService(WorkspaceStore(root),clarifier=clarifier)
            with TestClient(create_app(service)) as client:
                client.post("/v1/tasks",json={"task_id":"unready","mode":"manual"})
                imported=client.post("/v1/tasks/unready/input",json={"source":{"topic":"新品"}})
                self.assertEqual(imported.status_code,200)
                error=imported.json()["clarification"]["error"]
                self.assertEqual(error["code"],"runtime_unavailable")
                self.assertEqual(error["runtime_error_code"],"model_authentication_failed")
                self.assertEqual(error["failed_check"],"capability_contract")
                self.assertRegex(error["probe_id"],r"^runtime-probe-[0-9a-f]{32}$")
                retry=client.post(
                    "/v1/tasks/unready/clarifications/retry",
                    json={"idempotency_key":"unready-retry"},
                )
                self.assertEqual(retry.status_code,503)
                self.assertEqual(retry.json()["error"]["code"],"runtime_unavailable")
                self.assertEqual(retry.json()["error"]["diagnostic_id"],error["diagnostic_id"])
                self.assertEqual(client.get("/v1/tasks/unready/jobs").json()["jobs"],[])
                fallback=client.post("/v1/tasks/unready/clarifications/fallback",json={"confirm":True})
                self.assertEqual(fallback.status_code,200)
                self.assertEqual(fallback.json()["question_source"],"fallback")
            self.assertEqual(clarifier.calls,0)

    def test_classified_job_failure_degrades_readiness_and_keeps_public_error(self):
        class RateLimitedGateway:
            model="rate-limited-model"
            def set_audit_sink(self,_sink): pass
            def probe_capabilities(self): return {"strict_json_schema":True}
            def generate(self,_action,_payload,*,skill):
                raise GatewayError(
                    "模型服务请求过于频繁，请等待后重新探测",
                    code="model_rate_limited",
                    retryable=True,
                    retry_after_seconds=11,
                )
        with tempfile.TemporaryDirectory() as root:
            service=TaskService(WorkspaceStore(root),generator=RateLimitedGateway())
            with TestClient(create_app(service)) as client:
                client.post("/v1/tasks",json={"task_id":"rate-limited","mode":"manual"})
                client.post(
                    "/v1/tasks/rate-limited/input",
                    json={"source":{"goal":"演示","audience":"客户","topic":"方案"}},
                )
                created=client.post(
                    "/v1/tasks/rate-limited/jobs",
                    json={"operation":"narrative.generate","payload":{},"idempotency_key":"rate-key"},
                )
                self.assertEqual(created.status_code,202)
                job_id=created.json()["job_id"]
                deadline=time.monotonic()+2
                while time.monotonic()<deadline:
                    job=client.get(f"/v1/jobs/{job_id}").json()
                    if job["status"]=="failed": break
                    time.sleep(0.01)
                self.assertEqual(job["status"],"failed")
                self.assertEqual(job["error"]["code"],"model_rate_limited")
                self.assertTrue(job["error"]["retryable"])
                self.assertEqual(job["error"]["retry_after_seconds"],11)
                ready=client.get("/readyz")
                self.assertEqual(ready.status_code,503)
                self.assertEqual(ready.json()["model_capabilities"]["error"]["code"],"model_rate_limited")

    def test_task_and_job_scoped_audit_exports_are_filtered(self):
        self.service.create("audit-export")
        self.service.store.append_agent_audit({"audit_id":"a1","task_id":"audit-export","job_id":"job_match","events":[]})
        self.service.store.append_agent_audit({"audit_id":"a2","task_id":"audit-export","job_id":"job_other","events":[]})
        task=self.client.get("/v1/tasks/audit-export/agent-audits?job_id=job_match")
        self.assertEqual([item["audit_id"] for item in task.json()["audits"]],["a1"])

    def test_existing_api_contract_and_error_envelope(self):
        bad = self.client.post("/v1/tasks", json={"mode": "manual"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(set(bad.json()["error"]), {"code", "message", "diagnostic_id"})
        self.assertEqual(bad.json()["error"]["code"], "validation_error")

        malformed = self.client.post("/v1/tasks", content="[", headers={"content-type": "application/json"})
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json()["error"]["code"], "validation_error")
        wrong_method = self.client.put("/v1/tasks")
        self.assertEqual(wrong_method.status_code, 405)
        self.assertEqual(wrong_method.json()["error"]["code"], "method_not_allowed")

        self.client.post("/v1/tasks", json={"task_id": "compat", "mode": "manual"})
        imported = self.client.post("/v1/tasks/compat/input", json={"source": {"goal": "发布", "audience": "客户", "topic": "方案"}})
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(self.client.get("/v1/tasks/compat/input").status_code, 200)
        self.assertEqual(self.client.get("/v1/tasks/compat/planning").status_code, 200)
        listed = self.client.get("/v1/tasks").json()["tasks"]
        self.assertEqual([item["task_id"] for item in listed], ["compat"])

    def test_empty_resources_and_unstructured_markdown_return_clarifications(self):
        self.client.post("/v1/tasks", json={"task_id": "missing-input", "mode": "manual"})
        imported = self.client.post(
            "/v1/tasks/missing-input/input",
            json={"source": "这是一段尚未按任务卡格式整理的说明", "source_format": "markdown"},
        )
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["manifest"]["resources"], [])
        self.assertEqual(imported.json()["task_card"]["missing"], ["goal", "audience"])

        view = self.client.get("/v1/tasks/missing-input/input")
        self.assertEqual(view.status_code, 200)
        self.assertEqual(view.json()["source"], "这是一段尚未按任务卡格式整理的说明")
        self.assertEqual(view.json()["source_format"], "markdown")
        self.assertEqual(len(view.json()["clarification"]["questions"]), 2)
        self.assertEqual(view.json()["state"]["required_action"], "answer_clarifications")

    def test_preview_is_same_origin_sandbox_content_with_separate_csp(self):
        self.service.create("preview")
        digest = self.service.store.put_version(
            "preview", "deck", b'{}', {"html": "<!doctype html><style>body{color:red}</style><h1>Safe preview</h1>"}
        )
        response = self.client.get(f"/v1/tasks/preview/previews/{digest}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Safe preview", response.text)
        self.assertEqual(response.headers["x-frame-options"], "SAMEORIGIN")
        self.assertIn("script-src 'none'", response.headers["content-security-policy"])
        self.assertIn("style-src 'unsafe-inline'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_job_idempotency_sse_and_terminal_reconciliation(self):
        self.client.post("/v1/tasks", json={"task_id": "jobs", "mode": "manual"})
        self.client.post("/v1/tasks/jobs/input", json={"source": {"goal": "发布", "audience": "客户", "topic": "方案"}})
        payload = {"operation": "narrative.generate", "payload": {}, "idempotency_key": "intent-1"}
        first = self.client.post("/v1/tasks/jobs/jobs", json=payload)
        self.assertEqual(first.status_code, 202)
        job_id = first.json()["job_id"]

        second = self.client.post("/v1/tasks/jobs/jobs", json=payload)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["job_id"], job_id)
        conflict = self.client.post("/v1/tasks/jobs/jobs", json={**payload, "payload": {"prompt": "不同请求"}})
        self.assertEqual(conflict.status_code, 409)

        deadline = time.monotonic() + 3
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = self.client.get(f"/v1/jobs/{job_id}").json()
            if snapshot["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.02)
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(snapshot["result"]["task_id"], "jobs")
        self.assertNotIn("payload", snapshot)
        events = self.client.get(f"/v1/jobs/{job_id}/events?after=0")
        self.assertEqual(events.status_code, 200)
        self.assertIn("event: queued", events.text)
        self.assertIn("event: started", events.text)
        self.assertIn("event: succeeded", events.text)
        ids = [int(line.split(":", 1)[1]) for line in events.text.splitlines() if line.startswith("id:")]
        self.assertEqual(ids, sorted(set(ids)))


if __name__ == "__main__":
    unittest.main()
