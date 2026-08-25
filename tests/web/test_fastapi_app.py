import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from ppt_agent.config import ClarificationConfig
from ppt_agent.errors import GatewayError, RuntimeUnavailableError
from ppt_agent.service import TaskService
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

    def wait_for_startup(self,client,timeout=3):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            response=client.get("/v1/runtime/status").json()
            if response["startup_status"]!="starting":
                return response
            time.sleep(.01)
        self.fail("后台初始化未在时限内完成")

    def wait_for_job(self,client,job_id,timeout=3):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            job=client.get(f"/v1/jobs/{job_id}").json()
            if job["status"] in {"succeeded","failed","cancelled","interrupted"}:
                return job
            time.sleep(.01)
        self.fail(f"Job {job_id} 未在时限内结束")

    def test_health_shell_static_and_retired_legacy_routes(self):
        self.wait_for_startup(self.client)
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["web_runtime"], "fastapi")
        self.assertTrue(health.json()["runtime_ready"])
        self.assertEqual(health.json()["model_capabilities"]["status"],"on_demand")
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
        self.assertEqual(shell["latest_jobs"], [])

        self.assertIn("PPT Agent 工作台", self.client.get("/tasks/shell/outline").text)
        legacy = self.client.get("/legacy/tasks/shell")
        self.assertEqual(legacy.status_code, 404)
        self.assertNotIn("unsafe-inline", legacy.headers["content-security-policy"])

    def test_liveness_opens_before_local_prerequisites_finish(self):
        class BlockingCoreService(TaskService):
            def __init__(self,store):
                super().__init__(store)
                self.check_started=threading.Event()
                self.release_check=threading.Event()

            def initialize_generation_core(self):
                self.check_started.set()
                self.release_check.wait(3)
                return super().initialize_generation_core()

        with tempfile.TemporaryDirectory() as root:
            service=BlockingCoreService(WorkspaceStore(root))
            try:
                with TestClient(create_app(service)) as client:
                    self.assertTrue(service.check_started.wait(1))
                    self.assertEqual(client.get("/livez").status_code,200)
                    starting=client.get("/v1/runtime/status").json()
                    self.assertEqual(starting["startup_status"],"starting")
                    self.assertEqual(starting["startup_components"],{
                        "recovery":"ready","generation_core":"starting",
                    })
                    self.assertFalse(starting["runtime_ready"])
                    self.assertEqual(client.get("/readyz").status_code,503)
                    service.release_check.set()
                    ready=self.wait_for_startup(client)
                    self.assertEqual(ready["startup_status"],"ready")
                    self.assertTrue(ready["runtime_ready"])
                    self.assertEqual(client.get("/readyz").status_code,200)
            finally:
                service.release_check.set()

    def test_input_dispatches_clarification_directly(self):
        class DirectClarifier:
            model="clarification-only"
            def __init__(self):
                self.calls=0
                self.status_calls=0
            def set_audit_sink(self,_sink): pass
            def probe_capabilities(self):
                self.status_calls += 1
                raise AssertionError("status must not contact the model")
            def clarify(self,_payload):
                self.calls+=1
                return {"questions":[],"model":self.model}

        with tempfile.TemporaryDirectory() as root:
            clarifier=DirectClarifier()
            service=TaskService(WorkspaceStore(root),clarifier=clarifier)
            with TestClient(create_app(service)) as client:
                client.post("/v1/tasks",json={"task_id":"direct-cold","mode":"manual"})
                started=time.monotonic()
                imported=client.post("/v1/tasks/direct-cold/input",json={"source":{"topic":"新品"}})
                self.assertLess(time.monotonic()-started,1)
                job_id=imported.json()["clarification"]["job_id"]
                self.assertEqual(self.wait_for_job(client,job_id)["status"],"succeeded")
                self.assertTrue(client.get("/v1/tasks/direct-cold/input").json()["clarification"]["confirmed"])
                self.assertEqual(clarifier.calls,1)
                self.assertEqual(clarifier.status_calls,0)

    def test_startup_repairs_legacy_waiting_input_without_manual_click(self):
        class Clarifier:
            model="clarification-only"
            def __init__(self): self.calls=0
            def set_audit_sink(self,_sink): pass
            def clarify(self,_payload):
                self.calls+=1
                return {"questions":[],"model":self.model}

        with tempfile.TemporaryDirectory() as root:
            clarifier=Clarifier()
            service=TaskService(WorkspaceStore(root),clarifier=clarifier)
            service.create("legacy-wait")
            service.import_input("legacy-wait",{"topic":"新品"})
            service.wait_clarification_for_runtime(
                "legacy-wait",
                RuntimeUnavailableError(runtime_error_code="model_connection_error"),
            )
            with TestClient(create_app(service)) as client:
                deadline=time.monotonic()+3
                jobs=[]
                while time.monotonic()<deadline:
                    jobs=client.get("/v1/tasks/legacy-wait/jobs").json()["jobs"]
                    if jobs and jobs[0]["status"] in {"succeeded","failed"}: break
                    time.sleep(.01)
                self.assertEqual(len(jobs),1)
                self.assertEqual(jobs[0]["status"],"succeeded")
                self.assertTrue(client.get("/v1/tasks/legacy-wait/input").json()["clarification"]["confirmed"])
                self.assertEqual(clarifier.calls,1)

    def test_runtime_status_is_on_demand_and_has_no_model_recheck_routes(self):
        class DirectGateway:
            model="direct-model"
            calls=0
            def set_audit_sink(self,_sink): pass
            def probe_capabilities(self):
                self.calls += 1
                raise AssertionError("runtime status must not contact the model")

        with tempfile.TemporaryDirectory() as root:
            gateway=DirectGateway()
            service=TaskService(WorkspaceStore(root),generator=gateway)
            with TestClient(create_app(service)) as client:
                self.wait_for_startup(client)
                health=client.get("/v1/runtime/status").json()
                self.assertEqual(client.post("/v1/runtime/recheck").status_code,404)
                self.assertEqual(client.get("/v1/runtime/probes").status_code,404)
            self.assertTrue(health["model_capabilities"]["ready"])
            self.assertEqual(health["model_capabilities"]["status"],"on_demand")
            self.assertEqual(gateway.calls,0)

    def test_answer_completion_enqueues_next_configured_clarification_round(self):
        class MultiRoundClarifier:
            model="multi-round-model"
            def __init__(self): self.calls=[]
            def set_audit_sink(self,_sink): pass
            def clarify(self,payload):
                self.calls.append(payload)
                if payload["clarification_context"]["round"]==1:
                    return {"questions":[{"question_id":"q-audience","field_path":"audience","prompt":"受众是谁？","helper_text":"用于确定叙事重点","options":[],"allow_other":True,"blocking":True}],"model":self.model}
                return {"questions":[],"model":self.model}
        def wait_job(client,job_id):
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                job=client.get(f"/v1/jobs/{job_id}").json()
                if job["status"] in {"succeeded","failed","cancelled","interrupted"}: return job
                time.sleep(0.01)
            raise AssertionError(f"job {job_id} did not finish: {job}")
        with tempfile.TemporaryDirectory() as root:
            clarifier=MultiRoundClarifier()
            service=TaskService(WorkspaceStore(root),clarifier=clarifier,clarification_config=ClarificationConfig(max_questions_per_round=3,max_rounds=3,style="comprehensive"))
            with TestClient(create_app(service)) as client:
                client.post("/v1/tasks",json={"task_id":"rounds","mode":"manual"})
                imported=client.post("/v1/tasks/rounds/input",json={"source":"新品发布"})
                self.assertEqual(imported.status_code,200)
                first_job=wait_job(client,imported.json()["clarification"]["job_id"])
                self.assertEqual(first_job["status"],"succeeded")
                view=client.get("/v1/tasks/rounds/input").json()
                self.assertEqual(view["clarification"]["round"],1)
                question=view["clarification"]["details"][0]
                answered=client.post("/v1/tasks/rounds/clarifications/answers",json={"answers":{question["question_id"]:{"option":"Other","other":"管理层"}}})
                self.assertEqual(answered.status_code,200)
                body=answered.json()
                self.assertEqual(body["status"],"generating")
                self.assertEqual(body["round"],2)
                self.assertFalse(body["confirmed"])
                second_job=wait_job(client,body["job_id"])
                self.assertEqual(second_job["status"],"succeeded")
                view=client.get("/v1/tasks/rounds/input").json()
                self.assertTrue(view["clarification"]["confirmed"])
                self.assertEqual(view["clarification"]["round"],2)
                self.assertEqual(view["task_card"]["audience"],"管理层")
                self.assertEqual(view["state"]["status"],"ready")
                self.assertEqual(len(clarifier.calls),2)
                previous=clarifier.calls[1]["clarification_context"]["previous_qa"]
                self.assertEqual(previous[0]["answers"],{question["question_id"]:"管理层"})

    def test_clarifier_failure_persists_failed_job_and_preserves_fallback(self):
        class FailingClarifier:
            model="failing-model"
            calls=0
            def set_audit_sink(self,_sink): pass
            def clarify(self,_payload):
                self.calls += 1
                raise GatewayError(
                    "模型服务认证失败，请联系管理员检查凭据",
                    code="model_authentication_failed",
                )
        with tempfile.TemporaryDirectory() as root:
            clarifier=FailingClarifier()
            service=TaskService(WorkspaceStore(root),clarifier=clarifier)
            with TestClient(create_app(service)) as client:
                client.post("/v1/tasks",json={"task_id":"unready","mode":"manual"})
                imported=client.post("/v1/tasks/unready/input",json={"source":{"topic":"新品"}})
                self.assertEqual(imported.status_code,200)
                body=imported.json()
                self.assertRegex(body["clarification"]["job_id"],r"^job_")
                job=self.wait_for_job(client,body["clarification"]["job_id"])
                self.assertEqual(job["status"],"failed")
                view=client.get("/v1/tasks/unready/input").json()
                error=view["clarification"]["error"]
                self.assertEqual(view["clarification"]["status"],"failed")
                self.assertEqual(view["state"]["waiting_reason"],"clarification_failed")
                self.assertEqual(view["state"]["required_action"],"retry_clarification")
                self.assertEqual(error["code"],"model_authentication_failed")
                jobs=client.get("/v1/tasks/unready/jobs").json()["jobs"]
                self.assertEqual(len(jobs),1)
                self.assertEqual(jobs[0]["deadline_seconds"],90)
                fallback=client.post("/v1/tasks/unready/clarifications/fallback",json={"confirm":True})
                self.assertEqual(fallback.status_code,200)
                self.assertEqual(fallback.json()["question_source"],"fallback")
            self.assertEqual(clarifier.calls,1)

    def test_classified_job_failure_keeps_readiness_and_public_error(self):
        class RateLimitedGateway:
            model="rate-limited-model"
            def set_audit_sink(self,_sink): pass
            def generate(self,_action,_payload,*,skill):
                raise GatewayError(
                    "模型服务请求过于频繁，请等待后重试",
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
                self.assertEqual(ready.status_code,200)

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

    def test_quick_task_api_requires_explicit_final_slide_count(self):
        missing=self.client.post("/v1/tasks",json={"task_id":"quick-missing","mode":"quick"})
        self.assertEqual(missing.status_code,400)
        created=self.client.post("/v1/tasks",json={"task_id":"quick","mode":"quick","target_slide_count":2})
        self.assertEqual(created.status_code,201)
        self.assertEqual(created.json()["target_slide_count"],2)
        imported=self.client.post("/v1/tasks/quick/input",json={"source":{"goal":"发布","audience":"客户","topic":"方案"}})
        self.assertEqual(imported.status_code,200)
        shell=self.client.get("/v1/tasks/quick/shell").json()
        self.assertEqual((shell["task"]["mode"],shell["task"]["stage"]),("quick","sample"))

    def test_confirm_sample_unlocks_deck_before_generation_finishes_and_replays_job(self):
        self.service.create("deck-gate", "manual")
        self.service.import_input("deck-gate", {"goal":"发布","audience":"客户","topic":"方案","页数":3})
        self.service.generate_narrative("deck-gate")
        self.service.confirm_narrative("deck-gate")
        self.service.generate_outline("deck-gate")
        self.service.confirm_outline("deck-gate")
        self.service.generate_sample("deck-gate")

        response=self.client.post("/v1/tasks/deck-gate/samples/confirm",json={"auto_generate":True})
        self.assertEqual(response.status_code,200)
        first=response.json()
        self.assertEqual(first["state"]["stage"],"deck")
        self.assertEqual(first["deck_job"]["operation"],"deck.generate")
        shell=self.client.get("/v1/tasks/deck-gate/shell").json()
        self.assertEqual(next(item for item in shell["stages"] if item["id"]=="deck")["status"],"current")
        self.assertEqual(next(item for item in shell["stages"] if item["id"]=="sample")["status"],"completed")

        replay=self.client.post("/v1/tasks/deck-gate/samples/confirm",json={"auto_generate":True})
        self.assertEqual(replay.status_code,200)
        self.assertEqual(replay.json()["deck_job"]["job_id"],first["deck_job"]["job_id"])
        deadline=time.monotonic()+3
        job=None
        while time.monotonic()<deadline:
            # Poll the coordinator directly: TestClient itself is not safe for
            # concurrent requests while the executor is committing the deck.
            # A transient HTTP error used to raise KeyError and let teardown
            # delete the workspace under the still-running Job.
            job=self.client.app.state.job_service.get(first["deck_job"]["job_id"])
            if job["status"] in {"succeeded","failed","cancelled","interrupted"}:
                break
            time.sleep(.01)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"],"succeeded",job)

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
        history = self.client.get(f"/v1/jobs/{job_id}/event-history?after=0&limit=500")
        self.assertEqual(history.status_code, 200)
        history_events = history.json()["events"]
        self.assertEqual([item["seq"] for item in history_events], ids)
        self.assertEqual(len({(item["job_id"], item["seq"]) for item in history_events}), len(history_events))


if __name__ == "__main__":
    unittest.main()
