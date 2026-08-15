import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

from ppt_agent.agent_runtime import AgentRuntime, STAGE_OUTPUT_SCHEMAS, STAGE_PROMPTS, TOOLS
from ppt_agent.errors import GatewayError, GatewayUnknownResult, ValidationError
from ppt_agent.gateways import AgentGateway
from ppt_agent.model_clients import ModelToolCall, ModelTurn, OpenAIResponsesClient
from ppt_agent.skill_runtime import SkillRuntime


SCHEMA = {"name": "answer", "schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}}


class ScriptedClient:
    def __init__(self, turns):
        self.turns, self.inputs = list(turns), []

    def create(self, **kwargs):
        self.inputs.append(kwargs)
        return self.turns.pop(0)


class FailingClient:
    def __init__(self, error):
        self.error = error

    def create(self, **kwargs):
        raise self.error


def make_skill(root: Path, files: dict[str, bytes]):
    import hashlib
    for name, content in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    lock = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    (root / "SKILL_LOCK.json").write_text(json.dumps({"files": lock}))


class StageBSkillTests(unittest.TestCase):
    def test_builtin_is_locked_and_progressively_readable(self):
        skill = SkillRuntime.builtin()
        files = skill.list_skill_files()["files"]
        self.assertIn("SKILL.md", files); self.assertIn("references/checklist.md", files)
        result = skill.read_skill_file("SKILL.md")
        self.assertTrue(result["content"].strip()); self.assertEqual(len(result["sha256"]), 64)
        info = skill.get_asset_info("assets/template.html")
        self.assertEqual(info["media_type"], "text/html"); self.assertNotIn("content", info)

    def test_path_type_size_and_total_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); make_skill(root, {"SKILL.md": b"1234", "references/a.md": b"5678", "assets/x.bin": b"xx", "private.txt": b"no"})
            skill = SkillRuntime(root, max_file_bytes=4, max_total_bytes=6)
            for path in ("../secret", "/etc/passwd", "private.txt", "assets/x.bin"):
                with self.subTest(path=path), self.assertRaises(ValidationError): skill.read_skill_file(path)
            skill.read_skill_file("SKILL.md")
            with self.assertRaises(ValidationError): skill.read_skill_file("references/a.md")

    def test_symlink_and_tampered_lock_are_rejected(self):
        if not hasattr(os, "symlink"): self.skipTest("symlink unsupported")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "references").mkdir(); (root / "target").write_text("secret")
            os.symlink(root / "target", root / "references" / "a.md")
            make_skill(root, {"SKILL.md": b"ok"})
            lock = json.loads((root / "SKILL_LOCK.json").read_text()); lock["files"]["references/a.md"] = "0" * 64
            (root / "SKILL_LOCK.json").write_text(json.dumps(lock))
            with self.assertRaises(ValidationError): SkillRuntime(root)


class StageBAgentTests(unittest.TestCase):
    def test_invalid_tool_call_can_be_corrected_by_model(self):
        calls = [ModelToolCall("read_skill_file", '{"path":"../secret"}', "bad")]
        client = ScriptedClient([ModelTurn(None, "r1", calls), ModelTurn('{"markdown":"已纠正"}', "r2")])
        result = AgentRuntime(client, SkillRuntime.builtin()).run("narrative", {})
        self.assertEqual(result.value["markdown"], "已纠正")
        self.assertEqual(result.audit[2]["event"], "tool_error")
        self.assertIn("path_not_in_lock", str(client.inputs[1]["input"]))

    def test_clarification_has_no_tools_and_rejects_unsolicited_tool_calls(self):
        client=ScriptedClient([ModelTurn('{"questions":[]}',"r")])
        result=AgentRuntime(client,SkillRuntime.builtin()).run("clarification",{})
        self.assertEqual(result.value,{"questions":[]})
        self.assertEqual(client.inputs[0]["tools"],[])
        self.assertIn("不提供也不需要任何 Skill 工具",client.inputs[0]["input"][0]["content"])

        bad=ScriptedClient([ModelTurn(None,"r",(ModelToolCall("list_skill_files","{}","c"),))])
        with self.assertRaises(GatewayError) as caught:
            AgentRuntime(bad,SkillRuntime.builtin()).run("clarification",{})
        self.assertEqual(caught.exception.audit[-1]["reason"],"unauthorized_tool")

    def test_tool_error_budget_counts_complete_model_rounds(self):
        bad_calls=tuple(ModelToolCall("read_skill_file",'{"path":"../secret"}',f"bad-{index}") for index in range(3))
        recovered=ScriptedClient([ModelTurn(None,"r1",bad_calls),ModelTurn('{"markdown":"已恢复"}',"r2")])
        result=AgentRuntime(recovered,SkillRuntime.builtin()).run("narrative",{})
        self.assertEqual(result.value,{"markdown":"已恢复"})
        feedback=str(recovered.inputs[1]["input"])
        self.assertEqual(feedback.count("function_call_output"),3)
        self.assertEqual(sum(item.get("event")=="tool_error" for item in result.audit),3)
        self.assertEqual(sum(item.get("event")=="tool_error_round" for item in result.audit),1)

        exhausted=ScriptedClient([ModelTurn(None,"r1",bad_calls),ModelTurn(None,"r2",bad_calls)])
        with self.assertRaises(GatewayError) as caught:
            AgentRuntime(exhausted,SkillRuntime.builtin()).run("narrative",{})
        self.assertEqual(caught.exception.audit[-1]["reason"],"tool_error_limit")
        self.assertEqual(len(exhausted.inputs),2)
        self.assertEqual(sum(item.get("event")=="tool_error" for item in caught.exception.audit),6)

    def test_startup_probe_checks_schema_then_complete_tool_round(self):
        client=ScriptedClient([
            ModelTurn("OK","basic"),
            ModelTurn('{"questions":[]}',"clarification"),
            ModelTurn(None,"tools",(ModelToolCall("list_skill_files","{}","probe-call"),)),
            ModelTurn('{"markdown":"probe-ok"}',"final"),
        ])
        gateway=AgentGateway(client,skill=SkillRuntime.builtin())
        checks=gateway.probe_capabilities(probe_id="runtime-probe-test")
        self.assertEqual(checks,{"basic_response":True,"strict_json_schema":True,"tool_round_trip":True})
        self.assertEqual(client.inputs[0]["tools"],[])
        self.assertIsNone(client.inputs[0]["response_schema"])
        self.assertEqual(client.inputs[1]["tools"],[])
        self.assertEqual(client.inputs[2]["tools"],TOOLS)
        self.assertEqual(client.inputs[2]["tool_choice"],{"type":"function","name":"list_skill_files"})
        self.assertIsNone(client.inputs[2]["response_schema"])
        self.assertEqual(client.inputs[3]["tool_choice"],"none")
        self.assertEqual(client.inputs[3]["response_schema"],STAGE_OUTPUT_SCHEMAS["narrative"])
        self.assertIn("function_call_output",str(client.inputs[3]["input"]))
        self.assertEqual(gateway.last_probe_audit["probe_id"],"runtime-probe-test")
        self.assertEqual([event["status"] for event in gateway.last_probe_audit["events"]],["started","succeeded"]*3)

    def test_probe_failures_keep_failed_check_stable_code_and_secret_free_trace(self):
        basic=AgentGateway(ScriptedClient([ModelTurn(None,"empty")]),skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError) as caught:
            basic.probe_capabilities(probe_id="runtime-probe-basic")
        self.assertEqual(caught.exception.code,"probe_basic_response_failed")
        self.assertEqual(caught.exception.failed_check,"basic_response")

        gateway=AgentGateway(ScriptedClient([ModelTurn("OK","basic"),ModelTurn('{"wrong":1}',"bad"),ModelTurn('{"wrong":1}',"bad-again")]),skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError) as caught:
            gateway.probe_capabilities(probe_id="runtime-probe-schema")
        self.assertEqual(caught.exception.code,"probe_invalid_output")
        self.assertEqual(caught.exception.failed_check,"strict_json_schema")
        self.assertEqual(caught.exception.probe_id,"runtime-probe-schema")
        self.assertEqual(gateway.last_probe_audit["failed_check"],"strict_json_schema")
        serialized=json.dumps(gateway.last_probe_audit)
        self.assertNotIn('{"wrong":1}',serialized)

        missing=AgentGateway(ScriptedClient([ModelTurn("OK","basic"),ModelTurn('{"questions":[]}',"strict"),ModelTurn('{"markdown":"skipped"}',"no-tool")]),skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError) as caught:
            missing.probe_capabilities(probe_id="runtime-probe-tools")
        self.assertEqual(caught.exception.code,"probe_tool_call_missing")
        self.assertEqual(caught.exception.failed_check,"tool_round_trip")

        bad_calls=(ModelToolCall("shell","{}","bad"),)
        first_call=(ModelToolCall("list_skill_files","{}","first"),)
        broken=AgentGateway(ScriptedClient([ModelTurn("OK","basic"),ModelTurn('{"questions":[]}',"strict"),ModelTurn(None,"first",first_call),ModelTurn(None,"bad-1",bad_calls),ModelTurn(None,"bad-2",bad_calls)]),skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError) as caught:
            broken.probe_capabilities(probe_id="runtime-probe-broken-tools")
        self.assertEqual(caught.exception.code,"probe_tool_round_failed")
        self.assertEqual(caught.exception.failed_check,"tool_round_trip")

    def test_tool_error_codes_are_actionable_and_secret_free(self):
        self.assertEqual(AgentRuntime._tool_error_code("shell","denied"),"unauthorized_tool")
        self.assertEqual(AgentRuntime._tool_error_code("read_skill_file","Skill 路径越界"),"path_not_in_lock")
        self.assertEqual(AgentRuntime._tool_error_code("read_skill_file","Skill 累计读取超过上限"),"quota_exceeded")
    def test_tool_loop_schema_and_secret_free_audit(self):
        client = ScriptedClient([
            ModelTurn(None, "r1", (ModelToolCall("read_skill_file", json.dumps({"path": "SKILL.md"}), "c1"),)),
            ModelTurn('{"text":"done"}', "r2"),
        ])
        client.turns[-1] = ModelTurn('{"markdown":"done"}', "r2")
        result = AgentRuntime(client, SkillRuntime.builtin()).run("outline", {"topic": "secret-topic"})
        self.assertEqual(result.value, {"markdown": "done"}); self.assertEqual([x["event"] for x in result.audit], ["run", "model", "tool", "model", "terminal"])
        self.assertNotIn("secret-topic", json.dumps(result.audit)); self.assertNotIn("content", json.dumps(result.audit))
        self.assertIn("function_call_output", str(client.inputs[1]["input"])); self.assertEqual(client.inputs[0]["tools"], TOOLS)
        system = client.inputs[0]["input"][0]["content"]
        for denied in ("联网", "图片", "Shell", "文件写入", "自更新"):
            self.assertIn(denied, system)
        self.assertIn(STAGE_PROMPTS["outline"], system)

    def test_image_content_is_removed_from_every_nested_model_input(self):
        client = ScriptedClient([ModelTurn('{"html":"<html></html>"}', "r")])
        result = AgentRuntime(client, SkillRuntime.builtin()).run("deck", {
            "assets":{"resources://hero.png":"data:image/png;base64,SECRETBYTES"},
            "items":["ok", " DATA:IMAGE/JPEG;BASE64,MORESECRET"],
        })
        serialized=json.dumps(client.inputs,ensure_ascii=False)
        self.assertEqual(result.value["html"],"<html></html>")
        self.assertNotIn("SECRETBYTES",serialized); self.assertNotIn("MORESECRET",serialized)
        self.assertEqual(serialized.count("[image-content-removed]"),2)
        self.assertEqual(len(result.audit[0]["input_sha256"]),64)

    def test_each_stage_has_a_strict_default_output_schema(self):
        self.assertEqual(set(STAGE_PROMPTS), {"clarification", "narrative", "outline", "sample", "deck", "inspection"})
        for stage, schema in STAGE_OUTPUT_SCHEMAS.items():
            with self.subTest(stage=stage):
                self.assertTrue(schema["strict"]); self.assertFalse(schema["schema"]["additionalProperties"])

    def test_invalid_stage_tool_output_and_limits_fail_closed(self):
        with self.assertRaises(ValidationError): AgentRuntime(ScriptedClient([]), SkillRuntime.builtin()).run("publish", {})
        bad_tool = ScriptedClient([ModelTurn(None, "r", (ModelToolCall("shell", "{}", "c"),))])
        with self.assertRaises(GatewayError): AgentRuntime(bad_tool, SkillRuntime.builtin()).run("deck", {})
        bad_output = ScriptedClient([ModelTurn('{"wrong":1}', "r")])
        with self.assertRaises(GatewayError): AgentRuntime(bad_output, SkillRuntime.builtin()).run("deck", {})
        endless = ScriptedClient([ModelTurn(None, "r", (ModelToolCall("list_skill_files", "{}", f"c{i}"),)) for i in range(2)])
        with self.assertRaises(GatewayError): AgentRuntime(endless, SkillRuntime.builtin(), max_steps=2).run("sample", {})

    def test_schema_failure_gets_one_bounded_correction_with_field_path(self):
        client = ScriptedClient([
            ModelTurn('{"questions":[{"question_id":"q"}]}', "bad"),
            ModelTurn('{"questions":[]}', "fixed"),
        ])
        result = AgentRuntime(client, SkillRuntime.builtin()).run("clarification", {})
        self.assertEqual(result.value, {"questions": []})
        self.assertEqual(sum(x.get("event") == "schema_correction" for x in result.audit), 1)
        self.assertIn("output.questions[0] 缺少字段", str(client.inputs[1]["input"]))

        exhausted = ScriptedClient([ModelTurn('{"questions":[{}]}', "bad1"), ModelTurn('{"questions":[{}]}', "bad2")])
        with self.assertRaises(GatewayError) as caught:
            AgentRuntime(exhausted, SkillRuntime.builtin()).run("clarification", {})
        self.assertEqual(caught.exception.audit[-1]["reason"], "invalid_output")
        self.assertEqual(len(exhausted.inputs), 2)

    def test_lock_is_closed_and_rechecked_on_every_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); make_skill(root, {"SKILL.md": b"ok"})
            skill = SkillRuntime(root); (root / "references").mkdir(); (root / "references/unlocked.md").write_text("no")
            with self.assertRaises(ValidationError): skill.read_skill_file("references/unlocked.md")
            (root / "SKILL.md").write_text("tampered")
            with self.assertRaises(ValidationError): skill.read_skill_file("SKILL.md")
            with self.assertRaises(ValidationError): skill.read_skill_file("bad\0path")

    def test_schema_limits_deadline_and_failure_audit(self):
        with self.assertRaises(ValidationError):
            AgentRuntime(ScriptedClient([]), SkillRuntime.builtin()).run("deck", {}, response_schema=SCHEMA)
        arbitrary = ScriptedClient([ModelTurn('{"passed":false,"issues":[{"arbitrary_secret_field":"x"}]}', "secret-response")])
        runtime = AgentRuntime(arbitrary, SkillRuntime.builtin(), max_schema_corrections=0)
        with self.assertRaises(GatewayError) as caught: runtime.run("inspection", {})
        self.assertEqual(caught.exception.audit[-1]["reason"], "invalid_output")
        self.assertNotIn("secret-response", json.dumps(caught.exception.audit))
        huge = AgentRuntime(ScriptedClient([ModelTurn('{"markdown":"' + 'x' * 100 + '"}', "r")]), SkillRuntime.builtin(), max_output_bytes=32)
        with self.assertRaises(GatewayError) as caught: huge.run("outline", {})
        self.assertEqual(caught.exception.audit[-1]["reason"], "output_limit")
        calls = tuple(ModelToolCall("list_skill_files", "{}", f"secret-{i}") for i in range(2))
        limited = AgentRuntime(ScriptedClient([ModelTurn(None, "r", calls)]), SkillRuntime.builtin(), max_tool_calls=1)
        with self.assertRaises(GatewayError) as caught: limited.run("sample", {})
        self.assertEqual(caught.exception.audit[-1]["reason"], "tool_call_limit")

        ticks = iter([0, 0, 2])
        timed = AgentRuntime(ScriptedClient([ModelTurn('{"markdown":"late"}', "r")]), SkillRuntime.builtin(), timeout_seconds=1, clock=lambda: next(ticks))
        with self.assertRaises(GatewayError) as caught: timed.run("outline", {})
        self.assertEqual(caught.exception.audit[-1]["reason"], "deadline_exceeded")

    def test_unknown_gateway_result_preserves_type_public_semantics_and_audit(self):
        error = GatewayUnknownResult("模型请求结果未知")
        public = error.public()
        runtime = AgentRuntime(FailingClient(error), SkillRuntime.builtin())

        with self.assertRaises(GatewayUnknownResult) as caught:
            runtime.run("deck", {})

        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.public(), public)
        self.assertEqual(caught.exception.audit[-1]["reason"], "gateway_unknown_result")
        self.assertEqual(runtime.last_audit, caught.exception.audit)
        self.assertEqual(runtime.last_audit[0]["event"], "run")

    def test_gateway_error_keeps_original_exception_and_complete_failure_audit(self):
        error = GatewayError("模型服务调用失败")
        runtime = AgentRuntime(FailingClient(error), SkillRuntime.builtin())

        with self.assertRaises(GatewayError) as caught:
            runtime.run("outline", {})

        self.assertIs(caught.exception, error)
        self.assertEqual(caught.exception.code, "gateway_error")
        self.assertEqual(caught.exception.audit[-1], {"event": "terminal", "reason": "gateway_error", "tool_calls": 0})
        self.assertEqual(runtime.last_audit, caught.exception.audit)

    def test_client_extracts_function_calls(self):
        sdk = SimpleNamespace(); sdk.responses = sdk; requests=[]
        sdk.create = lambda **kwargs: (requests.append(kwargs) or SimpleNamespace(output_text="", id="r", output=[SimpleNamespace(type="function_call", name="read_skill_file", arguments='{"path":"SKILL.md"}', call_id="c")]))
        config = SimpleNamespace(model="m", api_key="k", base_url="https://example.com", timeout_seconds=1)
        choice={"type":"function","name":"read_skill_file"}
        turn = OpenAIResponsesClient(config, sdk_client=sdk).create(input=[],tools=TOOLS,tool_choice=choice)
        self.assertEqual(turn.tool_calls[0].name, "read_skill_file")
        self.assertEqual(requests[0]["tool_choice"],choice)

    def test_client_classifies_http_failures_and_only_audits_safe_metadata(self):
        expected = {
            400: ("model_request_invalid", False),
            401: ("model_authentication_failed", False),
            403: ("model_permission_denied", False),
            404: ("model_not_found", False),
            429: ("model_rate_limited", True),
            500: ("model_upstream_unavailable", True),
        }
        config = SimpleNamespace(model="m", api_key="secret-key", base_url="https://example.com", timeout_seconds=1)
        for status, (code, retryable) in expected.items():
            with self.subTest(status=status):
                request = httpx.Request("POST", "https://provider.example/v1/responses")
                response = httpx.Response(
                    status,
                    request=request,
                    headers={"x-request-id": "provider-request-secret", "retry-after": "17"},
                )
                failure = APIStatusError("raw provider failure secret", response=response, body={"secret": "raw-body"})
                sdk = SimpleNamespace(); sdk.responses = sdk
                sdk.create = lambda **_kwargs: (_ for _ in ()).throw(failure)
                with self.assertRaises(GatewayError) as caught:
                    OpenAIResponsesClient(config, sdk_client=sdk).create(input=[])
                public = caught.exception.public()["error"]
                audit = caught.exception.safe_audit_details()
                self.assertEqual(public["code"], code)
                self.assertEqual(public["retryable"], retryable)
                self.assertEqual(audit["http_status"], status)
                self.assertEqual(audit["sdk_exception_type"], "APIStatusError")
                self.assertEqual(
                    audit["provider_request_id_sha256"],
                    hashlib.sha256(b"provider-request-secret").hexdigest(),
                )
                self.assertNotIn("provider-request-secret", json.dumps({"public": public, "audit": audit}))
                self.assertNotIn("raw provider failure", json.dumps({"public": public, "audit": audit}))
                if status == 429:
                    self.assertEqual(public["retry_after_seconds"], 17)

    def test_client_distinguishes_timeout_connection_and_unknown_sdk_failures(self):
        config = SimpleNamespace(model="m", api_key="secret-key", base_url="https://example.com", timeout_seconds=1)
        request = httpx.Request("POST", "https://provider.example/v1/responses")
        cases = [
            (APITimeoutError(request=request), GatewayUnknownResult, "model_timeout", "timeout"),
            (APIConnectionError(request=request), GatewayUnknownResult, "model_connection_error", "connection"),
            (RuntimeError("raw sdk secret"), GatewayError, "gateway_error", "sdk_error"),
        ]
        for failure, error_type, code, category in cases:
            with self.subTest(code=code):
                sdk = SimpleNamespace(); sdk.responses = sdk
                sdk.create = lambda **_kwargs: (_ for _ in ()).throw(failure)
                with self.assertRaises(error_type) as caught:
                    OpenAIResponsesClient(config, sdk_client=sdk).create(input=[])
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.safe_audit_details()["category"], category)
                self.assertNotIn("raw sdk secret", json.dumps(caught.exception.public()))


if __name__ == "__main__": unittest.main()
