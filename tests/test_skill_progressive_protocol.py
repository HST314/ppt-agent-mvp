import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from ppt_agent.agent_runtime import AgentRuntime
from ppt_agent.errors import GatewayError, ValidationError
from ppt_agent.execution import execution_scope
from ppt_agent.model_clients import ModelToolCall, ModelTurn
from ppt_agent.skill_runtime import ActiveSkillResolver


def write_skill(root: Path, *, script: str = "print('ok')\n") -> Path:
    skill = root / "progressive"
    (skill / "references").mkdir(parents=True)
    (skill / "assets").mkdir()
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: progressive-skill\ndescription: Progressive test skill\n---\n"
        "Read [the guide](references/guide.md) only when it is relevant.\n",
        encoding="utf-8",
    )
    (skill / "references" / "guide.md").write_text("guide-body", encoding="utf-8")
    (skill / "assets" / "pixel.bin").write_bytes(b"\x00\x01")
    (skill / "scripts" / "check.py").write_text(script, encoding="utf-8")
    return skill


class Client:
    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []

    def create(self, **kwargs):
        self.inputs.append(kwargs)
        return self.turns.pop(0)


def call(name: str, arguments: dict, call_id: str) -> ModelTurn:
    return ModelTurn(None, call_id, (ModelToolCall(name, json.dumps(arguments), call_id),))


class ProgressiveSkillProtocolTests(unittest.TestCase):
    def runtime(self, root: Path, client: Client, **limits) -> AgentRuntime:
        skill = ActiveSkillResolver(root, "progressive").runtime(**limits.pop("skill_limits", {}))
        return AgentRuntime(client, skill, **limits)

    def test_open_reading_is_not_limited_to_four_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = write_skill(root)
            for index in range(5):
                (skill / "references" / f"guide-{index}.md").write_text(f"guide-{index}", encoding="utf-8")
            turns = [call("read_skill_file", {"path": "SKILL.md"}, "entry")]
            turns.extend(
                call("read_skill_file", {"path": f"references/guide-{index}.md"}, f"guide-{index}")
                for index in range(5)
            )
            turns.append(ModelTurn('{"markdown":"done"}', "final"))
            result = self.runtime(root, Client(turns), max_steps=8, max_provider_calls=8).run("narrative", {})
            self.assertEqual(result.value, {"markdown": "done"})
            self.assertEqual(result.audit[-1]["unique_skill_files"], 6)

    def test_large_text_can_be_read_in_cached_utf8_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root)
            runtime = ActiveSkillResolver(root, "progressive").runtime(max_file_bytes=4, max_total_bytes=12)
            first = runtime.read_skill_file("references/guide.md", offset=0, limit=4)
            second = runtime.read_skill_file("references/guide.md", offset=4, limit=4)
            third = runtime.read_skill_file("references/guide.md", offset=8, limit=4)
            cached = runtime.read_skill_file("references/guide.md", offset=0, limit=4)
            self.assertEqual(first["content"] + second["content"] + third["content"], "guide-body")
            self.assertTrue(third["eof"])
            self.assertTrue(cached["cached"])
            self.assertEqual(runtime.total_bytes, len("guide-body"))

    def test_entry_is_forced_before_generic_progressive_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root)
            client = Client([
                call("read_skill_file", {"path": "SKILL.md"}, "entry"),
                call("read_skill_file", {"path": "references/guide.md"}, "guide"),
                ModelTurn('{"markdown":"done"}', "final"),
            ])

            result = self.runtime(root, client).run("narrative", {})

            self.assertEqual(result.value, {"markdown": "done"})
            first_tools = client.inputs[0]["tools"]
            self.assertEqual([tool["name"] for tool in first_tools], ["read_skill_file"])
            self.assertEqual(first_tools[0]["parameters"]["properties"]["path"]["enum"], ["SKILL.md"])
            second_names = {tool["name"] for tool in client.inputs[1]["tools"]}
            self.assertEqual(
                second_names,
                {"list_skill_files", "read_skill_file", "get_asset_info", "run_skill_script"},
            )
            terminal = result.audit[-1]
            self.assertTrue(terminal["skill_entry_read"])
            self.assertEqual(terminal["applied_skill_files"], ["SKILL.md", "references/guide.md"])

    def test_missing_entry_gets_exactly_one_protocol_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root)
            client = Client([
                ModelTurn('{"markdown":"too early"}', "early"),
                call("read_skill_file", {"path": "SKILL.md"}, "entry"),
                ModelTurn('{"markdown":"fixed"}', "final"),
            ])
            result = self.runtime(root, client).run("narrative", {})
            self.assertEqual(result.value["markdown"], "fixed")
            self.assertEqual(
                sum(item.get("event") == "skill_entry_protocol_correction" for item in result.audit),
                1,
            )

            rejected = Client([
                ModelTurn('{"markdown":"early"}', "early-1"),
                ModelTurn('{"markdown":"still early"}', "early-2"),
            ])
            with self.assertRaises(GatewayError) as caught:
                self.runtime(root, rejected).run("narrative", {})
            self.assertEqual(caught.exception.code, "agent_skill_entry_missing")

    def test_status_events_only_expose_tool_and_valid_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root)
            client = Client([
                call("read_skill_file", {"path": "SKILL.md"}, "entry"),
                ModelTurn('{"markdown":"done"}', "final"),
            ])
            events = []
            with execution_scope(
                lambda: False,
                time.monotonic() + 5,
                lambda step, message, details: events.append((step, message, details)),
            ):
                self.runtime(root, client).run("narrative", {})
            completed = next(item for item in events if item[0] == "skill_completed")
            self.assertEqual(completed[2]["tool_name"], "read_skill_file")
            self.assertEqual(completed[2]["tool_path"], "SKILL.md")
            serialized = json.dumps(events, ensure_ascii=False)
            self.assertNotIn("Progressive test skill", serialized)
            self.assertNotIn("guide-body", serialized)

    def test_script_validation_rejects_escape_and_oversized_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root)
            runtime = ActiveSkillResolver(root, "progressive").runtime()
            with self.assertRaises(ValidationError):
                runtime.run_skill_script("../check.py")
            with self.assertRaises(ValidationError):
                runtime.run_skill_script("scripts/check.py", args=["x" * 1025])

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is required")
    def test_script_sandbox_has_no_credentials_network_or_skill_writes(self):
        script = """
import os, pathlib, socket
checks = []
checks.append(os.environ.get('MODEL_API_KEY') is None)
try:
    pathlib.Path('/skill/SKILL.md').write_text('tampered')
    checks.append(False)
except OSError:
    checks.append(True)
try:
    socket.create_connection(('1.1.1.1', 53), timeout=0.1)
    checks.append(False)
except OSError:
    checks.append(True)
pathlib.Path('scratch.txt').write_text('temporary')
checks.append(pathlib.Path('scratch.txt').read_text() == 'temporary')
print(','.join(str(item).lower() for item in checks))
raise SystemExit(0 if all(checks) else 9)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = write_skill(root, script=script)
            entry = skill / "SKILL.md"
            before = hashlib.sha256(entry.read_bytes()).hexdigest()
            runtime = ActiveSkillResolver(root, "progressive").runtime()
            result = runtime.run_skill_script("scripts/check.py")
            self.assertTrue(result["script_succeeded"], result)
            self.assertEqual(result["stdout"].strip(), "true,true,true,true")
            self.assertEqual(hashlib.sha256(entry.read_bytes()).hexdigest(), before)

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is required")
    def test_script_nonzero_timeout_and_output_limit_are_advisory(self):
        cases = [
            ("raise SystemExit(7)\n", {}, "script_nonzero_exit"),
            ("import time; time.sleep(2)\n", {"script_timeout_seconds": 0.1}, "script_timeout"),
            ("print('x' * 4096)\n", {"max_script_output_bytes": 1024}, "script_output_limit"),
        ]
        for source, limits, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_skill(root, script=source)
                runtime = ActiveSkillResolver(root, "progressive").runtime(**limits)
                result = runtime.run_skill_script("scripts/check.py")
                self.assertTrue(result["ok"])
                self.assertFalse(result["script_succeeded"])
                self.assertEqual(result["advisory"]["code"], expected)

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is required")
    def test_script_failure_does_not_block_agent_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, script="raise SystemExit(5)\n")
            client = Client([
                call("read_skill_file", {"path": "SKILL.md"}, "entry"),
                call("run_skill_script", {"path": "scripts/check.py", "args": ["--self-check"]}, "script"),
                ModelTurn('{"markdown":"delivery continues"}', "final"),
            ])
            result = self.runtime(root, client).run("narrative", {})
            self.assertEqual(result.value["markdown"], "delivery continues")
            script_audit = next(item for item in result.audit if item.get("tool") == "run_skill_script")
            self.assertFalse(script_audit["script_succeeded"])
            self.assertEqual(script_audit["advisory_code"], "script_nonzero_exit")


if __name__ == "__main__":
    unittest.main()
