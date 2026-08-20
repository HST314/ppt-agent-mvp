import tempfile
import unittest

from ppt_agent.gateways import AgentGateway
from ppt_agent.model_clients import ModelToolCall, ModelTurn
from ppt_agent.service import TaskService
from ppt_agent.skill_runtime import SkillRuntime
from ppt_agent.store import WorkspaceStore


class PassingModelInspector:
    def __init__(self):
        self.evidence = None

    def inspect(self, outline, html, *, browser_evidence=None):
        self.evidence = browser_evidence
        return {"passed": True, "issues": [], "model": "passing-model"}


class BlockingBrowserInspector:
    def inspect(self, html, expected_slide_ids):
        return {
            "available": True,
            "passed": False,
            "engine": "chromium",
            "engine_version": "test-browser",
            "viewport": {"width": 1280, "height": 720},
            "issues": [{
                "issue_id": "browser-overflow-test",
                "severity": "blocker",
                "level": "element",
                "code": "content_out_of_bounds",
                "message": "元素超出页面安全边界",
                "slide_id": expected_slide_ids[0],
                "element_id": "body",
                "evidence": "DOM geometry exceeds slide by 42.0px",
                "suggestion": "收紧内容",
            }],
            "slides": [],
        }


class ScriptedClient:
    def __init__(self, turns):
        self.turns = list(turns)
        self.inputs = []

    def create(self, **kwargs):
        self.inputs.append(kwargs)
        return self.turns.pop(0)


class P0HybridInspectionTests(unittest.TestCase):
    def test_agent_inspection_must_apply_checklist_and_receives_browser_evidence(self):
        browser_evidence = {
            "available": True,
            "passed": True,
            "engine": "chromium",
            "viewport": {"width": 1280, "height": 720},
            "issues": [],
            "slides": [],
        }
        client = ScriptedClient([
            ModelTurn(None, "skill", (ModelToolCall(
                "read_skill_file",
                '{"path":"references/checklist.md"}',
                "checklist-call",
            ),)),
            ModelTurn('{"passed":true,"issues":[]}', "final"),
        ])
        gateway = AgentGateway(client, skill=SkillRuntime.builtin())

        result = gateway.inspect("## [slide-1] 大纲", "<!doctype html>", browser_evidence=browser_evidence)

        self.assertTrue(result["passed"])
        first = client.inputs[0]
        self.assertEqual(first["tool_choice"], {"type":"function", "name":"read_skill_file"})
        self.assertEqual(
            first["tools"][0]["parameters"]["properties"]["path"]["enum"],
            ["references/checklist.md"],
        )
        self.assertIn('"browser_evidence"', first["input"][1]["content"])
        self.assertEqual(
            gateway.runtime.last_audit[-1]["applied_skill_files"],
            ["references/checklist.md"],
        )

    def test_model_green_is_overridden_by_browser_blocker_and_evidence_is_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            model = PassingModelInspector()
            service = TaskService(
                WorkspaceStore(root),
                inspector=model,
                browser_inspector=BlockingBrowserInspector(),
            )
            service.create("hybrid", "manual")
            service.import_input("hybrid", {"goal":"发布", "audience":"客户", "topic":"方案", "页数":3})
            service.generate_narrative("hybrid"); service.confirm_narrative("hybrid")
            service.generate_outline("hybrid"); service.confirm_outline("hybrid")
            service.generate_sample("hybrid"); service.confirm_sample("hybrid")
            service.generate_deck("hybrid")

            result = service.run_inspection("hybrid", 0)

            self.assertFalse(result["report"]["passed"])
            self.assertEqual(result["report"]["issues"][0]["code"], "content_out_of_bounds")
            self.assertFalse(result["delivery_allowed"])
            self.assertTrue(model.evidence["available"])
            metadata = result["report"]["metadata"]
            self.assertTrue(metadata["includes_browser_render"])
            self.assertFalse(metadata["browser_evidence"]["passed"])
            self.assertEqual(metadata["browser_evidence"]["issue_count"], 1)


if __name__ == "__main__":
    unittest.main()
