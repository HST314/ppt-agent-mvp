import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

from ppt_agent.agent_runtime import AgentRuntime, STAGE_OUTPUT_SCHEMAS, STAGE_PROVIDER_SCHEMAS, STAGE_PROMPTS, TOOLS
from ppt_agent.errors import GatewayError, GatewayUnknownResult, ValidationError
from ppt_agent.gateways import AgentGateway
from ppt_agent.model_clients import ModelToolCall, ModelTurn, OpenAIResponsesClient
from ppt_agent.skill_runtime import SkillRuntime


SCHEMA = {"name": "answer", "schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}}
OUTLINE_VALUE = {"slides": [{"title": "完成", "purpose": "验证契约", "content_markdown": "- 内容", "resource_uris": []}]}
OUTLINE_JSON = json.dumps(OUTLINE_VALUE, ensure_ascii=False)


def skill_entry_turn(call_id="skill-entry"):
    return ModelTurn(None, call_id, (ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', call_id),))


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
    files = dict(files)
    if not files.get("SKILL.md", b"").startswith(b"---"):
        files["SKILL.md"] = b"---\nname: test-skill\ndescription: Test skill.\n---\n\n" + files.get("SKILL.md", b"")
    for name, content in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)


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
            root = Path(tmp); make_skill(root, {"SKILL.md": b"entry", "references/a.md": b"1234", "references/b.md": b"5678", "assets/x.bin": b"xx", "private.txt": b"no"})
            skill = SkillRuntime(root, max_file_bytes=4, max_total_bytes=6)
            for path in ("../secret", "/etc/passwd", "private.txt", "assets/x.bin"):
                with self.subTest(path=path), self.assertRaises(ValidationError): skill.read_skill_file(path)
            skill.read_skill_file("references/a.md")
            with self.assertRaises(ValidationError): skill.read_skill_file("references/b.md")

    def test_tool_path_prefixes_are_normalized_but_whitelist_stays_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); make_skill(root, {"SKILL.md": b"ok", "references/a.md": b"a"})
            skill = SkillRuntime(root)
            for variant in ("./SKILL.md", "/SKILL.md", "test-skill/SKILL.md", f"./{root.name}/references/a.md"):
                with self.subTest(variant=variant):
                    self.assertTrue(skill.read_skill_file(variant)["content"])
            for hostile in ("../SKILL_LOCK.json", "guizang-ppt/../SKILL_LOCK.json", "private.txt", "SKILL_LOCK.json"):
                with self.subTest(hostile=hostile), self.assertRaises(ValidationError):
                    skill.read_skill_file(hostile)

    def test_whitelist_error_lists_valid_paths_for_self_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); make_skill(root, {"SKILL.md": b"ok", "references/a.md": b"a"})
            skill = SkillRuntime(root)
            with self.assertRaises(ValidationError) as caught:
                skill.read_skill_file("references/missing.md")
            message = caught.exception.message
            self.assertIn("SKILL.md", message); self.assertIn("references/a.md", message)

    def test_symlink_is_rejected_without_requiring_a_lock_file(self):
        if not hasattr(os, "symlink"): self.skipTest("symlink unsupported")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "references").mkdir(); (root / "target").write_text("secret")
            os.symlink(root / "target", root / "references" / "a.md")
            make_skill(root, {"SKILL.md": b"ok"})
            with self.assertRaises(ValidationError): SkillRuntime(root)


class StageBAgentTests(unittest.TestCase):
    def test_invalid_tool_call_can_be_corrected_by_model(self):
        calls = [ModelToolCall("read_skill_file", '{"path":"../secret"}', "bad")]
        client = ScriptedClient([ModelTurn(None, "r1", calls), skill_entry_turn(), ModelTurn('{"markdown":"已纠正"}', "r2")])
        result = AgentRuntime(client, SkillRuntime.builtin()).run("narrative", {})
        self.assertEqual(result.value["markdown"], "已纠正")
        self.assertEqual(result.audit[2]["event"], "tool_error")
        self.assertIn("path_not_in_lock", str(client.inputs[1]["input"]))
        self.assertIn("只能调用 read_skill_file", str(client.inputs[1]["input"]))
        self.assertEqual([tool["name"] for tool in client.inputs[1]["tools"]], ["read_skill_file"])
        self.assertIn("SKILL.md", client.inputs[1]["tools"][0]["parameters"]["properties"]["path"]["enum"])

    def test_tool_audit_records_requested_path_hash_and_normalized_path(self):
        requested = "guizang-ppt/references/planning-summary.md"
        calls = [ModelToolCall("read_skill_file", json.dumps({"path":requested}), "c1")]
        client = ScriptedClient([skill_entry_turn(), ModelTurn(None, "r1", calls), ModelTurn('{"markdown":"ok"}', "r2")])
        result = AgentRuntime(client, SkillRuntime.builtin()).run("narrative", {})
        tool_event = next(e for e in result.audit if e.get("path") == "references/planning-summary.md")
        self.assertEqual(tool_event["requested_path_sha256"], hashlib.sha256(requested.encode()).hexdigest())
        self.assertEqual(tool_event["path"], "references/planning-summary.md")

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
        recovered=ScriptedClient([ModelTurn(None,"r1",bad_calls),skill_entry_turn(),ModelTurn('{"markdown":"已恢复"}',"r2")])
        result=AgentRuntime(recovered,SkillRuntime.builtin()).run("narrative",{})
        self.assertEqual(result.value,{"markdown":"已恢复"})
        feedback=recovered.inputs[1]["input"]
        self.assertEqual(sum(item.get("type") == "function_call_output" and str(item.get("call_id", "")).startswith("bad-") for item in feedback),3)
        self.assertEqual(sum(item.get("event")=="tool_error" for item in result.audit),3)
        self.assertEqual(sum(item.get("event")=="tool_error_round" for item in result.audit),1)

        exhausted=ScriptedClient([ModelTurn(None,"r1",bad_calls),ModelTurn(None,"r2",bad_calls)])
        with self.assertRaises(GatewayError) as caught:
            AgentRuntime(exhausted,SkillRuntime.builtin()).run("narrative",{})
        self.assertEqual(caught.exception.audit[-1]["reason"],"tool_error_limit")
        self.assertEqual(caught.exception.code,"stage_tool_contract_error")
        self.assertEqual(len(exhausted.inputs),2)
        self.assertEqual(sum(item.get("event")=="tool_error" for item in caught.exception.audit),6)

    def test_startup_probe_checks_schema_then_complete_tool_round(self):
        client=ScriptedClient([
            ModelTurn("OK","basic"),
            ModelTurn(OUTLINE_JSON,"outline-schema"),
            skill_entry_turn("probe-call"),
            ModelTurn('{"markdown":"probe-ok"}',"final"),
        ])
        gateway=AgentGateway(client,skill=SkillRuntime.builtin())
        checks=gateway.probe_capabilities(probe_id="runtime-probe-test")
        self.assertEqual(checks,{"basic_response":True,"strict_json_schema":True,"tool_round_trip":True})
        self.assertEqual(client.inputs[0]["tools"],[])
        self.assertIsNone(client.inputs[0]["response_schema"])
        self.assertEqual(client.inputs[1]["tools"],[])
        self.assertEqual(client.inputs[1]["response_schema"],STAGE_PROVIDER_SCHEMAS["outline"])
        self.assertEqual([tool["name"] for tool in client.inputs[2]["tools"]],["read_skill_file"])
        self.assertEqual(client.inputs[2]["tools"][0]["parameters"]["properties"]["path"]["enum"],["SKILL.md"])
        self.assertEqual(client.inputs[2]["tool_choice"],{"type":"function","name":"read_skill_file"})
        self.assertIsNone(client.inputs[2]["response_schema"])
        self.assertEqual(client.inputs[3]["tool_choice"],"none")
        self.assertEqual(client.inputs[3]["response_schema"],STAGE_PROVIDER_SCHEMAS["narrative"])
        self.assertIn("function_call_output",str(client.inputs[3]["input"]))
        self.assertEqual(gateway.last_probe_audit["probe_id"],"runtime-probe-test")
        self.assertEqual([event["status"] for event in gateway.last_probe_audit["events"]],["started","succeeded"]*3)

    def test_basic_probe_retries_one_sdk_parse_failure_then_recovers(self):
        turns=[
            ModelTurn("OK","basic"),
            ModelTurn(OUTLINE_JSON,"schema"),
            skill_entry_turn("probe-call"),
            ModelTurn('{"markdown":"probe-ok"}',"final"),
        ]

        class RetryClient(ScriptedClient):
            def __init__(self): super().__init__(turns); self.calls=0
            def create(self,**kwargs):
                self.calls+=1
                if self.calls==1:
                    raise GatewayError(
                        "SDK parse failed",
                        audit_details={"category":"sdk_error","sdk_exception_type":"AttributeError","retryable":False},
                    )
                return super().create(**kwargs)

        client=RetryClient()
        gateway=AgentGateway(client,skill=SkillRuntime.builtin())
        checks=gateway.probe_capabilities(probe_id="runtime-probe-retry")

        self.assertTrue(all(checks.values()))
        self.assertEqual(client.calls,5)
        retry=gateway.last_probe_audit["events"][1]
        self.assertEqual(retry["status"],"retrying")
        self.assertEqual(retry["attempt"],1)
        self.assertEqual(retry["max_attempts"],2)

    def test_basic_probe_sdk_parse_retry_is_bounded(self):
        class PersistentSDKFailure:
            def __init__(self): self.calls=0
            def create(self,**_kwargs):
                self.calls+=1
                raise AttributeError("response parser failed")

        client=PersistentSDKFailure()
        gateway=AgentGateway(client,skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError) as caught:
            gateway.probe_capabilities(probe_id="runtime-probe-bounded")
        self.assertEqual(client.calls,2)
        self.assertEqual(caught.exception.failed_check,"basic_response")
        self.assertEqual([event["status"] for event in gateway.last_probe_audit["events"]],["started","retrying","failed"])

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

        missing=AgentGateway(ScriptedClient([ModelTurn("OK","basic"),ModelTurn(OUTLINE_JSON,"strict"),ModelTurn('{"markdown":"skipped"}',"no-tool")]),skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError) as caught:
            missing.probe_capabilities(probe_id="runtime-probe-tools")
        self.assertEqual(caught.exception.code,"probe_tool_call_missing")
        self.assertEqual(caught.exception.failed_check,"tool_round_trip")
        self.assertEqual(caught.exception.probe_phase,"tool_request")
        self.assertEqual(caught.exception.terminal_reason,"capability_probe_failed")
        self.assertEqual(caught.exception.tool_calls,0)

        final_invalid=AgentGateway(ScriptedClient([
            ModelTurn("OK","basic"),
            ModelTurn(OUTLINE_JSON,"strict"),
            skill_entry_turn("call"),
            ModelTurn("not-json","bad-final"),
            ModelTurn("still-not-json","bad-final-again"),
        ]),skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError) as caught:
            final_invalid.probe_capabilities(probe_id="runtime-probe-final-invalid")
        self.assertEqual(caught.exception.code,"probe_tool_final_invalid_output")
        self.assertEqual(caught.exception.failed_check,"tool_round_trip")
        self.assertEqual(caught.exception.probe_phase,"tool_final_output")
        self.assertEqual(caught.exception.terminal_reason,"invalid_output")
        self.assertEqual(caught.exception.tool_calls,1)
        self.assertEqual(final_invalid.last_probe_audit["events"][-1]["tool_calls"],1)

        rejected=AgentGateway(ScriptedClient([
            ModelTurn("OK","basic"),
            ModelTurn(OUTLINE_JSON,"strict"),
            skill_entry_turn("call"),
            GatewayError("provider rejected tool output",code="model_request_invalid"),
        ]),skill=SkillRuntime.builtin())
        original_create=rejected.client.create
        def create_or_raise(**kwargs):
            turn=original_create(**kwargs)
            if isinstance(turn,Exception): raise turn
            return turn
        rejected.client.create=create_or_raise
        with self.assertRaises(GatewayError) as caught:
            rejected.probe_capabilities(probe_id="runtime-probe-result-rejected")
        self.assertEqual(caught.exception.code,"probe_tool_round_failed")
        self.assertEqual(caught.exception.probe_phase,"tool_result")
        self.assertEqual(caught.exception.underlying_code,"model_request_invalid")
        self.assertEqual(caught.exception.tool_calls,1)

        bad_calls=(ModelToolCall("shell","{}","bad"),)
        first_call=(ModelToolCall("read_skill_file",'{"path":"SKILL.md"}',"first"),)
        broken=AgentGateway(ScriptedClient([ModelTurn("OK","basic"),ModelTurn(OUTLINE_JSON,"strict"),ModelTurn(None,"first",first_call),ModelTurn(None,"bad-1",bad_calls),ModelTurn(None,"bad-2",bad_calls)]),skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError) as caught:
            broken.probe_capabilities(probe_id="runtime-probe-broken-tools")
        self.assertEqual(caught.exception.code,"probe_tool_round_failed")
        self.assertEqual(caught.exception.failed_check,"tool_round_trip")

        classified=AgentGateway(FailingClient(GatewayError(
            "模型服务认证失败",
            code="model_authentication_failed",
            audit_details={"category":"authentication","sdk_exception_type":"APIStatusError","retryable":False},
        )),skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError) as caught:
            classified.probe_capabilities(probe_id="runtime-probe-classified")
        self.assertEqual(caught.exception.code,"model_authentication_failed")
        self.assertEqual(caught.exception.failed_check,"basic_response")

    def test_tool_error_codes_are_actionable_and_secret_free(self):
        self.assertEqual(AgentRuntime._tool_error_code("shell","denied"),"unauthorized_tool")
        self.assertEqual(AgentRuntime._tool_error_code("read_skill_file","Skill 路径越界"),"path_not_in_lock")
        self.assertEqual(AgentRuntime._tool_error_code("read_skill_file","Skill 累计读取超过上限"),"quota_exceeded")
    def test_tool_loop_schema_and_secret_free_audit(self):
        client = ScriptedClient([
            skill_entry_turn("c1"),
            ModelTurn('{"text":"done"}', "r2"),
        ])
        client.turns[-1] = ModelTurn(OUTLINE_JSON, "r2")
        result = AgentRuntime(client, SkillRuntime.builtin()).run("outline", {"topic": "secret-topic"})
        self.assertEqual(result.value, OUTLINE_VALUE); self.assertEqual([x["event"] for x in result.audit], ["run", "model", "tool", "model", "terminal"])
        self.assertNotIn("secret-topic", json.dumps(result.audit)); self.assertNotIn("content", json.dumps(result.audit))
        self.assertIn("function_call_output", str(client.inputs[1]["input"])); self.assertEqual([tool["name"] for tool in client.inputs[0]["tools"]], ["read_skill_file"])
        system = client.inputs[0]["input"][0]["content"]
        for denied in ("联网", "图片", "Shell", "文件写入", "自更新"):
            self.assertIn(denied, system)
        self.assertIn(STAGE_PROMPTS["outline"], system)

    def test_planning_stage_tools_paths_and_prompt_are_the_same_contract(self):
        finals = {
            "narrative": '{"markdown":"narrative-ok"}',
            "outline": OUTLINE_JSON,
            "inspection": '{"passed":true,"issues":[]}',
        }
        for stage, final in finals.items():
            with self.subTest(stage=stage):
                client = ScriptedClient([
                    skill_entry_turn("call"),
                    ModelTurn(final, "final"),
                ])
                AgentRuntime(client, SkillRuntime.builtin()).run(stage, {})
                tools = client.inputs[0]["tools"]
                self.assertEqual([tool["name"] for tool in tools], ["read_skill_file"])
                self.assertEqual(
                    tools[-1]["parameters"]["properties"]["path"]["enum"],
                    ["SKILL.md"],
                )
                system = client.inputs[0]["input"][0]["content"]
                self.assertIn("Skill 发现", system)
                self.assertIn("必须首先调用 read_skill_file 完整读取 SKILL.md", system)
                followup_names = {tool["name"] for tool in client.inputs[1]["tools"]}
                self.assertEqual(followup_names, {"list_skill_files", "read_skill_file", "get_asset_info", "run_skill_script"})

    def test_planning_stage_reads_one_summary_and_idempotently_deduplicates_it(self):
        calls = (
            ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "one"),
            ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "two"),
        )
        client = ScriptedClient([ModelTurn(None, "tools", calls), ModelTurn(OUTLINE_JSON, "final")])

        result = AgentRuntime(client, SkillRuntime.builtin()).run("outline", {})

        self.assertEqual(result.value, OUTLINE_VALUE)
        self.assertEqual(sum(item.get("event") == "tool" for item in result.audit), 2)
        cached = [item for item in result.audit if item.get("event") == "tool" and item.get("repeated")]
        self.assertEqual(len(cached), 1)
        self.assertFalse(any(item.get("event") == "tool_error" for item in result.audit))
        self.assertEqual(client.inputs[1]["tools"], [])
        self.assertEqual(client.inputs[1]["tool_choice"], "none")

    def test_planning_stage_rejects_a_hidden_asset_tool(self):
        client = ScriptedClient([
            ModelTurn(None, "asset", (ModelToolCall("get_asset_info", '{"path":"assets/template.html"}', "asset-call"),)),
            skill_entry_turn(),
            ModelTurn('{"markdown":"recovered"}', "final"),
        ])

        result = AgentRuntime(client, SkillRuntime.builtin()).run("narrative", {})

        self.assertEqual(result.value, {"markdown": "recovered"})
        tool_error = next(item for item in result.audit if item.get("event") == "tool_error")
        self.assertEqual(tool_error["error_code"], "unauthorized_tool")
        self.assertEqual([tool["name"] for tool in client.inputs[1]["tools"]], ["read_skill_file"])
        self.assertIn("SKILL.md", client.inputs[1]["tools"][0]["parameters"]["properties"]["path"]["enum"])

    def test_mixed_tool_batch_recovery_uses_one_remaining_path_contract(self):
        first_batch = (
            ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "initial-read"),
            ModelToolCall("get_asset_info", '{"path":"assets/template.html"}', "hidden-tool"),
        )
        recovery_batch = (ModelToolCall("read_skill_file", '{"path":"references/planning-summary.md"}', "recovery-read"),)
        client = ScriptedClient([
            ModelTurn(None, "mixed", first_batch),
            ModelTurn(None, "recovery", recovery_batch),
            ModelTurn(OUTLINE_JSON, "final"),
        ])

        result = AgentRuntime(client, SkillRuntime.builtin()).run("outline", {})

        recovery_tools = client.inputs[1]["tools"]
        self.assertEqual([tool["name"] for tool in recovery_tools], ["read_skill_file"])
        self.assertIsNone(client.inputs[1]["tool_choice"])
        recovery_prompts = [
            item["content"] for item in client.inputs[1]["input"]
            if item.get("role") == "user" and "受限恢复轮" in item.get("content", "")
        ]
        recovery_prompt = recovery_prompts[0]
        self.assertIn("references/planning-summary.md", recovery_prompt)
        second_round = [item for item in result.audit if item.get("step") == 2 and item.get("event") in {"tool", "tool_error"}]
        self.assertEqual(
            [(item["event"], item.get("error_code"), item.get("path")) for item in second_round],
            [("tool", None, "references/planning-summary.md")],
        )
        self.assertEqual([tool["name"] for tool in client.inputs[2]["tools"]], ["read_skill_file"])
        self.assertIsNone(client.inputs[2]["tool_choice"])
        self.assertNotIn("references/planning-summary.md", recovery_prompts[-1])

    def test_duplicate_entry_read_is_cached_and_forces_final_output(self):
        duplicate = (
            ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "initial-read"),
            ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "duplicate"),
        )
        client = ScriptedClient([ModelTurn(None, "mixed", duplicate), ModelTurn(OUTLINE_JSON, "final")])
        result = AgentRuntime(client, SkillRuntime.builtin()).run("outline", {})
        self.assertEqual(result.value, OUTLINE_VALUE)
        self.assertEqual(client.inputs[1]["tools"], [])
        self.assertEqual(client.inputs[1]["tool_choice"], "none")
        self.assertEqual(result.audit[-1]["repeated_skill_reads"], 1)

    def test_image_content_is_removed_from_every_nested_model_input(self):
        client = ScriptedClient([
            skill_entry_turn("skill-call"),
            ModelTurn('{"slides":[{"slide_id":"slide-1","html":"<section class=\\"slide\\" id=\\"slide-1\\" data-slide-id=\\"slide-1\\"><p>ok</p></section>"}]}', "r"),
        ])
        result = AgentRuntime(client, SkillRuntime.builtin()).run("deck", {
            "slide_ids":["slide-1"],
            "assets":{"resources://hero.png":"data:image/png;base64,SECRETBYTES"},
            "items":["ok", " DATA:IMAGE/JPEG;BASE64,MORESECRET"],
        })
        serialized=json.dumps(client.inputs,ensure_ascii=False)
        self.assertEqual(result.value["slides"][0]["slide_id"],"slide-1")
        self.assertNotIn("SECRETBYTES",serialized); self.assertNotIn("MORESECRET",serialized)
        self.assertTrue(all(json.dumps(request,ensure_ascii=False).count("[image-content-removed]")==2 for request in client.inputs))
        self.assertEqual(len(result.audit[0]["input_sha256"]),64)

    def test_each_stage_has_a_strict_default_output_schema(self):
        self.assertEqual(set(STAGE_PROMPTS), {"clarification", "narrative", "outline", "sample", "deck", "inspection"})
        for stage, schema in STAGE_OUTPUT_SCHEMAS.items():
            with self.subTest(stage=stage):
                self.assertTrue(schema["strict"]); self.assertFalse(schema["schema"]["additionalProperties"])

    def test_provider_outline_schema_omits_local_only_constraints(self):
        local = json.dumps(STAGE_OUTPUT_SCHEMAS["outline"], sort_keys=True)
        provider = json.dumps(STAGE_PROVIDER_SCHEMAS["outline"], sort_keys=True)
        for keyword in ("minLength", "minItems", "uniqueItems"):
            self.assertIn(keyword, local)
            self.assertNotIn(keyword, provider)

        client = ScriptedClient([
            skill_entry_turn(),
            ModelTurn('{"slides":[]}', "empty"),
            ModelTurn(OUTLINE_JSON, "fixed"),
        ])
        result = AgentRuntime(client, SkillRuntime.builtin()).run("outline", {})
        self.assertEqual(result.value, OUTLINE_VALUE)
        self.assertIsNone(client.inputs[0]["response_schema"])
        self.assertEqual(client.inputs[1]["response_schema"], STAGE_PROVIDER_SCHEMAS["outline"])
        self.assertEqual(sum(item.get("event") == "schema_correction" for item in result.audit), 1)

        compatible = ScriptedClient([skill_entry_turn(), ModelTurn(OUTLINE_JSON, "compatible")])
        AgentRuntime(compatible, SkillRuntime.builtin()).run(
            "outline", {}, response_schema=STAGE_OUTPUT_SCHEMAS["outline"]
        )
        self.assertEqual(compatible.inputs[1]["response_schema"], STAGE_PROVIDER_SCHEMAS["outline"])

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

    def test_rendering_fragment_failure_gets_one_bounded_technical_correction(self):
        bad=json.dumps({"slides":[{"slide_id":"slide-1","html":"<html><body>wrong shell</body></html>"}]})
        fixed=json.dumps({"slides":[{"slide_id":"slide-1","html":"```html\n<section class=\"slide\"><h1>ok</h1></section>\n```"}]})
        skill_turn=skill_entry_turn("skill-call")
        client=ScriptedClient([skill_turn,ModelTurn(bad,"bad"),ModelTurn(fixed,"fixed")])
        result=AgentRuntime(client,SkillRuntime.builtin()).run("sample",{"slide_ids":["slide-1"]})
        fragment=result.value["slides"][0]["html"]
        self.assertTrue(fragment.startswith('<section data-slide-id="slide-1" id="slide-1" class="slide">'))
        self.assertEqual(sum(item.get("event")=="technical_correction" for item in result.audit),1)
        self.assertIn("technical_correction",str(client.inputs[2]["input"]))
        self.assertEqual(client.inputs[2]["tool_choice"],"none")

        exhausted=ScriptedClient([skill_turn,ModelTurn(bad,"bad-1"),ModelTurn(bad,"bad-2")])
        with self.assertRaises(GatewayError) as caught:
            AgentRuntime(exhausted,SkillRuntime.builtin()).run("deck",{"slide_ids":["slide-1"]})
        self.assertEqual(caught.exception.code,"agent_invalid_output")
        self.assertEqual(caught.exception.audit[-1]["reason"],"invalid_output")

    def test_snapshot_is_closed_and_rechecked_on_every_read(self):
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
        arbitrary = ScriptedClient([
            skill_entry_turn("skill-call"),
            ModelTurn('{"passed":false,"issues":[{"arbitrary_secret_field":"x"}]}', "secret-response"),
        ])
        runtime = AgentRuntime(arbitrary, SkillRuntime.builtin(), max_schema_corrections=0)
        with self.assertRaises(GatewayError) as caught: runtime.run("inspection", {})
        self.assertEqual(caught.exception.audit[-1]["reason"], "invalid_output")
        self.assertNotIn("secret-response", json.dumps(caught.exception.audit))
        huge = AgentRuntime(ScriptedClient([skill_entry_turn(), ModelTurn(json.dumps({"slides":[{"title":"t","purpose":"p","content_markdown":"x"*100,"resource_uris":[]}]}), "r")]), SkillRuntime.builtin(), max_output_bytes=32)
        with self.assertRaises(GatewayError) as caught: huge.run("outline", {})
        self.assertEqual(caught.exception.audit[-1]["reason"], "output_limit")
        calls = tuple(ModelToolCall("list_skill_files", "{}", f"secret-{i}") for i in range(2))
        limited = AgentRuntime(ScriptedClient([ModelTurn(None, "r", calls)]), SkillRuntime.builtin(), max_tool_calls=1)
        with self.assertRaises(GatewayError) as caught: limited.run("sample", {})
        self.assertEqual(caught.exception.audit[-1]["reason"], "tool_call_limit")

        ticks = iter([0, 0, 2])
        timed = AgentRuntime(ScriptedClient([skill_entry_turn(), ModelTurn(OUTLINE_JSON, "r")]), SkillRuntime.builtin(), timeout_seconds=1, clock=lambda: next(ticks))
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

    def test_narrative_recovers_empty_markdown_from_responses_message_content(self):
        sdk = SimpleNamespace(); sdk.responses = sdk; sdk.calls = 0; sdk.requests = []
        def create(**kwargs):
            sdk.calls += 1; sdk.requests.append(kwargs)
            if sdk.calls == 1:
                return SimpleNamespace(
                    output_text="",
                    id="r-entry",
                    output=[SimpleNamespace(type="function_call", name="read_skill_file", arguments='{"path":"SKILL.md"}', call_id="entry")],
                )
            markdown = "" if sdk.calls == 2 else "# 已恢复叙事"
            return SimpleNamespace(
                output_text="",
                id=f"r-{sdk.calls}",
                output=[SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text=json.dumps({"markdown":markdown},ensure_ascii=False))],
                )],
            )
        sdk.create = create
        config = SimpleNamespace(model="m", api_key="k", base_url="https://example.com", timeout_seconds=1, structured_output="prompt")
        client = OpenAIResponsesClient(config, sdk_client=sdk)

        result = AgentRuntime(client, SkillRuntime.builtin()).run("narrative", {})

        self.assertEqual(result.value, {"markdown":"# 已恢复叙事"})
        self.assertEqual(sdk.calls, 3)
        correction = next(item for item in result.audit if item.get("event") == "schema_correction")
        self.assertEqual(correction["reason"], "schema_validation")
        self.assertIn("长度不足", str(sdk.requests[2]["input"]))
        self.assertEqual(sdk.requests[2]["tool_choice"], "none")

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
                if code != "gateway_error":
                    self.assertEqual(caught.exception.safe_audit_details()["attempts"], 1)
                    self.assertEqual(caught.exception.safe_audit_details()["transport_phase"], "unknown")
                    self.assertEqual(caught.exception.safe_audit_details()["result_certainty"], "unknown")
                self.assertNotIn("raw sdk secret", json.dumps(caught.exception.public()))

    def test_capability_probe_identity_ignores_stage_budgets_but_not_credentials(self):
        common={
            "provider":"openai_responses",
            "model":"shared-model",
            "api_key":"secret-key",
            "base_url":"https://provider.example/v1",
            "timeout_seconds":1,
            "structured_output":"auto",
        }
        generation=OpenAIResponsesClient(SimpleNamespace(**common,stage_budgets={"deck":"generation-only"}))
        inspection=OpenAIResponsesClient(SimpleNamespace(**common,stage_budgets={}))
        other_key=OpenAIResponsesClient(SimpleNamespace(**{**common,"api_key":"different-key"}))

        self.assertEqual(generation.capability_probe_key(),inspection.capability_probe_key())
        self.assertNotEqual(generation.capability_probe_key(),other_key.capability_probe_key())
        serialized=json.dumps({"generation":generation.capability_probe_key(),"inspection":inspection.capability_probe_key()})
        self.assertNotIn("secret-key",serialized)

    def test_client_does_not_replay_a_wrapper_only_timeout_with_unknown_result(self):
        config = SimpleNamespace(model="m", api_key="secret-key", base_url="https://example.com", timeout_seconds=1)
        request = httpx.Request("POST", "https://provider.example/v1/responses")
        sdk = SimpleNamespace(); sdk.responses = sdk; sdk.calls = 0
        def create(**_kwargs):
            sdk.calls += 1
            raise APITimeoutError(request=request)
        sdk.create = create

        with self.assertRaises(GatewayUnknownResult) as caught:
            OpenAIResponsesClient(config, sdk_client=sdk).create(input=[])

        self.assertEqual(caught.exception.code,"model_timeout")
        self.assertEqual(sdk.calls,1)

    def test_client_retries_only_a_proven_pre_dispatch_connection_failure(self):
        config = SimpleNamespace(model="m", api_key="secret-key", base_url="https://example.com", timeout_seconds=1)
        request = httpx.Request("POST", "https://provider.example/v1/responses")
        sdk = SimpleNamespace(); sdk.responses = sdk; sdk.calls = 0
        def create(**_kwargs):
            sdk.calls += 1
            if sdk.calls == 1:
                failure=APIConnectionError(request=request)
                failure.__cause__=httpx.ConnectError("connect failed",request=request)
                raise failure
            return SimpleNamespace(output_text="recovered", id="r", output=[])
        sdk.create = create

        turn=OpenAIResponsesClient(config,sdk_client=sdk).create(input=[])

        self.assertEqual(turn.text,"recovered")
        self.assertEqual(sdk.calls,2)

    def test_exhausted_pre_dispatch_failures_are_known_unsent_and_retryable(self):
        config = SimpleNamespace(model="m", api_key="secret-key", base_url="https://example.com", timeout_seconds=1)
        request = httpx.Request("POST", "https://provider.example/v1/responses")
        sdk = SimpleNamespace(); sdk.responses = sdk; sdk.calls = 0
        def create(**_kwargs):
            sdk.calls += 1
            failure=APIConnectionError(request=request)
            failure.__cause__=httpx.ConnectError("connect failed",request=request)
            raise failure
        sdk.create=create

        with self.assertRaises(GatewayError) as caught:
            OpenAIResponsesClient(config,sdk_client=sdk).create(input=[])

        self.assertNotIsInstance(caught.exception,GatewayUnknownResult)
        self.assertEqual(caught.exception.code,"model_connection_unavailable")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.safe_audit_details()["transport_phase"],"pre_dispatch")
        self.assertEqual(caught.exception.safe_audit_details()["result_certainty"],"not_sent")
        self.assertEqual(sdk.calls,2)

    def test_client_never_replays_a_read_failure_after_dispatch(self):
        config=SimpleNamespace(model="m",api_key="secret-key",base_url="https://example.com",timeout_seconds=1)
        request=httpx.Request("POST","https://provider.example/v1/responses")
        sdk=SimpleNamespace(); sdk.responses=sdk; sdk.calls=0
        def create(**_kwargs):
            sdk.calls+=1
            failure=APIConnectionError(request=request)
            failure.__cause__=httpx.ReadError("read failed",request=request)
            raise failure
        sdk.create=create

        with self.assertRaises(GatewayUnknownResult) as caught:
            OpenAIResponsesClient(config,sdk_client=sdk).create(input=[])

        self.assertEqual(caught.exception.code,"model_connection_error")
        self.assertEqual(caught.exception.safe_audit_details()["result_certainty"],"unknown")
        self.assertEqual(sdk.calls,1)

    def test_request_snapshot_can_limit_visible_skill_files(self):
        skill = SkillRuntime.builtin()
        allowed = frozenset({"SKILL.md"})
        self.assertEqual(skill.list_skill_files(allowed_files=allowed)["files"], ["SKILL.md"])
        with self.assertRaises(ValidationError):
            skill.dispatch("read_skill_file", {"path": "references/themes.md"}, allowed_files=allowed)

    def test_client_structured_output_modes_control_text_format(self):
        schema = {"name": "narrative", "strict": True, "schema": {"type": "object"}}
        for mode in ("auto", "json_schema", "prompt"):
            with self.subTest(mode=mode):
                config = SimpleNamespace(model="m", api_key="k", base_url="https://example.com", timeout_seconds=1, structured_output=mode)
                sdk = SimpleNamespace(); sdk.responses = sdk; sdk.seen = []
                sdk.create = lambda **kwargs: (sdk.seen.append(kwargs) or SimpleNamespace(output_text="ok", id="r", output=[]))
                client = OpenAIResponsesClient(config, sdk_client=sdk)
                client.create(input=[], response_schema=schema)
                if mode == "prompt":
                    self.assertNotIn("text", sdk.seen[0])
                else:
                    self.assertEqual(sdk.seen[0]["text"], {"format": {"type": "json_schema", **schema}})

    def test_client_auto_mode_falls_back_once_on_400_and_caches(self):
        config = SimpleNamespace(model="m", api_key="k", base_url="https://example.com", timeout_seconds=1, structured_output="auto")
        request = httpx.Request("POST", "https://provider.example/v1/responses")
        failure = APIStatusError(
            "unsupported parameter: text.format",
            response=httpx.Response(400, request=request),
            body={"error":{"message":"Unsupported parameter: text.format","param":"text.format"}},
        )
        sdk = SimpleNamespace(); sdk.responses = sdk; sdk.seen = []
        def create(**kwargs):
            sdk.seen.append(kwargs)
            if len(sdk.seen) == 1:
                raise failure
            return SimpleNamespace(output_text="ok", id="r", output=[])
        sdk.create = create
        schema = {"name": "narrative", "strict": True, "schema": {"type": "object"}}
        client = OpenAIResponsesClient(config, sdk_client=sdk)

        turn = client.create(input=[], response_schema=schema)
        self.assertEqual(turn.text, "ok")
        self.assertEqual(len(sdk.seen), 2)
        self.assertIn("text", sdk.seen[0])
        self.assertNotIn("text", sdk.seen[1])

        client.create(input=[], response_schema=schema)
        self.assertEqual(len(sdk.seen), 3)
        self.assertNotIn("text", sdk.seen[2])

        failing = SimpleNamespace(); failing.responses = failing
        failing.create = lambda **_kwargs: (_ for _ in ()).throw(failure)
        strict = OpenAIResponsesClient(SimpleNamespace(model="m", api_key="k", base_url="https://example.com", timeout_seconds=1, structured_output="json_schema"), sdk_client=failing)
        with self.assertRaises(GatewayError) as caught:
            strict.create(input=[], response_schema=schema)
        self.assertEqual(caught.exception.code, "model_request_invalid")

    def test_client_schema_400_never_falls_back_or_poisons_capability_cache(self):
        config = SimpleNamespace(model="m", api_key="k", base_url="https://example.com", timeout_seconds=1, structured_output="auto")
        request = httpx.Request("POST", "https://provider.example/v1/responses")
        invalid_schema = APIStatusError(
            "invalid schema",
            response=httpx.Response(400, request=request),
            body={"error":{"message":"Invalid schema for response_format 'outline': 'uniqueItems' is not permitted in schema.","code":"invalid_json_schema","param":"text.format.schema"}},
        )
        sdk = SimpleNamespace(); sdk.responses = sdk; sdk.seen = []
        def create(**kwargs):
            sdk.seen.append(kwargs)
            if len(sdk.seen) == 1:
                raise invalid_schema
            return SimpleNamespace(output_text="ok", id="r", output=[])
        sdk.create = create
        client = OpenAIResponsesClient(config, sdk_client=sdk)
        schema = STAGE_PROVIDER_SCHEMAS["outline"]

        with self.assertRaises(GatewayError) as caught:
            client.create(input=[], response_schema=schema)
        self.assertEqual(caught.exception.code, "model_schema_invalid")
        self.assertEqual(len(sdk.seen), 1)

        client.create(input=[], response_schema=schema)
        self.assertEqual(len(sdk.seen), 2)
        self.assertIn("text", sdk.seen[1])

    def test_runtime_parses_fenced_and_prose_wrapped_json(self):
        for text in ('```json\n{"markdown":"fenced"}\n```', '说明文字 {"markdown":"prose"} 结束'):
            with self.subTest(text=text[:12]):
                client = ScriptedClient([skill_entry_turn(), ModelTurn(text, "r")])
                result = AgentRuntime(client, SkillRuntime.builtin()).run("narrative", {})
                self.assertEqual(result.value, {"markdown": "fenced" if "fenced" in text else "prose"})

    def test_system_prompt_spells_out_the_output_contract(self):
        client = ScriptedClient([skill_entry_turn(), ModelTurn('{"markdown":"ok"}', "r")])
        AgentRuntime(client, SkillRuntime.builtin()).run("narrative", {})
        system = client.inputs[0]["input"][0]["content"]
        self.assertIn("输出契约", system)
        self.assertIn('"markdown"', system)
        self.assertIn("additionalProperties", system)


if __name__ == "__main__": unittest.main()
