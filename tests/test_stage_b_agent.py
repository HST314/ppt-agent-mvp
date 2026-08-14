import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ppt_agent.agent_runtime import AgentRuntime, STAGE_OUTPUT_SCHEMAS, STAGE_PROMPTS, TOOLS
from ppt_agent.errors import GatewayError, GatewayUnknownResult, ValidationError
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
        self.assertIn("tool_validation_error", str(client.inputs[1]["input"]))
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
        self.assertEqual(set(STAGE_PROMPTS), {"narrative", "outline", "sample", "deck", "inspection"})
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
        runtime = AgentRuntime(arbitrary, SkillRuntime.builtin())
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
        sdk = SimpleNamespace(); sdk.responses = sdk
        sdk.create = lambda **kwargs: SimpleNamespace(output_text="", id="r", output=[SimpleNamespace(type="function_call", name="read_skill_file", arguments='{"path":"SKILL.md"}', call_id="c")])
        config = SimpleNamespace(model="m", api_key="k", base_url="https://example.com", timeout_seconds=1)
        turn = OpenAIResponsesClient(config, sdk_client=sdk).create(input=[])
        self.assertEqual(turn.tool_calls[0].name, "read_skill_file")


if __name__ == "__main__": unittest.main()
