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


class PassingBrowserInspector:
    def inspect(self, html, expected_slide_ids):
        return {
            "available": True,
            "passed": True,
            "engine": "chromium",
            "engine_version": "test-browser",
            "viewport": {"width": 1280, "height": 720},
            "issues": [],
            "slides": [],
        }


class PlaceholderBuilder:
    def build(self, outline, **context):
        from ppt_agent.p4 import render

        html = render(
            outline,
            context["slide_ids"],
            context.get("rules"),
            context.get("exceptions"),
            context.get("assets"),
        )
        return html.replace(
            "</section>",
            '<p data-element-id="unbound-kpi">覆盖 XX 条业务线，日均处理 XXX 次对话</p></section>',
            1,
        )


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
                '{"path":"SKILL.md"}',
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
            ["SKILL.md"],
        )
        self.assertIn('"browser_evidence"', first["input"][1]["content"])
        self.assertEqual(
            gateway.runtime.last_audit[-1]["applied_skill_files"],
            ["SKILL.md"],
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

    def test_deterministic_semantic_findings_are_advisory_to_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(
                WorkspaceStore(root),
                inspector=PassingModelInspector(),
                browser_inspector=PassingBrowserInspector(),
                builder=PlaceholderBuilder(),
            )
            service.create("content-gate", "manual")
            service.import_input("content-gate", {"goal":"发布", "audience":"管理层", "topic":"试点复盘", "页数":3})
            service.generate_narrative("content-gate"); service.confirm_narrative("content-gate")
            service.generate_outline("content-gate"); service.confirm_outline("content-gate")
            service.generate_sample("content-gate"); service.confirm_sample("content-gate")
            service.generate_deck("content-gate")

            result = service.run_inspection("content-gate", 0)

            placeholders = [item for item in result["report"]["issues"] if item["code"] == "placeholder_token"]
            self.assertGreaterEqual(len(placeholders), 2)
            self.assertTrue(all(item["severity"] == "warning" for item in placeholders))
            self.assertTrue(all(item["source"] == "semantic_deterministic" for item in placeholders))
            self.assertFalse(result["report"]["passed"])
            self.assertTrue(result["delivery_allowed"])
            self.assertFalse(result["blocking_issues"])
            metadata = result["report"]["metadata"]
            self.assertTrue(metadata["includes_content_quality"])
            self.assertTrue(metadata["includes_browser_render"])
            self.assertEqual(metadata["quality_checks"]["semantic_deterministic"]["issue_count"], len(placeholders))
            self.assertEqual(metadata["quality_checks"]["semantic_model"]["issue_count"], 0)
            self.assertEqual(metadata["quality_checks"]["technical_browser"]["issue_count"], 0)


if __name__ == "__main__":
    unittest.main()
