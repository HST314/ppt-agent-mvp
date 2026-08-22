from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ppt_agent.design_contract import (
    PresentationTechnicalContract,
    build_presentation_technical_contract,
    scope_presentation_technical_contract,
)
from ppt_agent.gateways import BoundaryCheckedHtml
from ppt_agent.p4 import assemble_presentation, validate_html
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


HASH_A = "a" * 64
HASH_B = "b" * 64
INTENT = {
    "style_summary": "高对比几何叙事",
    "color_strategy": "深紫背景配青色强调",
    "typography_strategy": "粗体标题与紧凑正文形成层级",
    "layout_principles": ["固定节奏", "同类信息复用结构"],
    "rationale": "让代表页与全稿保持一致且易于快速扫描",
}
ASSETS = {"css": ".slide{background:#24114d;color:#f8fafc}.slide h1{color:#5eead4}"}


def technical_contract(slide_ids=None):
    return build_presentation_technical_contract(
        task_id="task",
        input_snapshot_hash=HASH_A,
        outline_hash=HASH_B,
        slide_ids=slide_ids or ["slide-1", "slide-2", "slide-3"],
        created_at="2026-08-22T00:00:00+00:00",
    )


class PresentationTechnicalContractTests(unittest.TestCase):
    def test_contract_contains_only_framework_technical_facts(self):
        contract = technical_contract()
        parsed = PresentationTechnicalContract.parse(contract)
        self.assertEqual(parsed.slide_ids, ("slide-1", "slide-2", "slide-3"))
        self.assertEqual(contract["canvas"], {"width": 1280, "height": 720, "aspect_ratio": "16:9"})
        forbidden = {"style_id", "template_id", "theme_id", "allowed_layouts", "slide_contracts", "semantic_classes"}
        self.assertFalse(forbidden.intersection(contract))

    def test_scoped_batch_recomputes_identity_without_adding_design_choices(self):
        contract = technical_contract()
        scoped = scope_presentation_technical_contract(contract, ["slide-2"])
        self.assertEqual(scoped["slide_ids"], ["slide-2"])
        self.assertNotEqual(scoped["contract_id"], contract["contract_id"])
        self.assertEqual(scoped["canvas"], contract["canvas"])


class GenericAssemblerTests(unittest.TestCase):
    def test_assembler_preserves_agent_classes_and_shared_css(self):
        contract = technical_contract(["slide-1"])
        fragment = '<section class="slide constellation" id="slide-1" data-slide-id="slide-1"><h1>自定义设计</h1></section>'
        result = assemble_presentation(
            [fragment],
            technical_contract=contract,
            contract_hash=HASH_A,
            design_intent=INTENT,
            shared_assets=ASSETS,
        )
        validate_html(result, ["slide-1"])
        self.assertIn("constellation", result)
        self.assertIn(ASSETS["css"], result)
        self.assertIn('name="presentation-technical-contract"', result)
        self.assertNotIn("data-layout=", result)
        self.assertNotIn("ppt-template", result)


class IntentBuilder:
    def __init__(self):
        self.calls = []

    def build(self, _outline, **context):
        self.calls.append(context)
        if context["action"] == "deck":
            assert context["confirmed_design_intent"] == INTENT
            assert context["confirmed_shared_assets"] == ASSETS
        sections = [
            f'<section class="slide constellation" id="{slide_id}" data-slide-id="{slide_id}"><h1>{slide_id}</h1></section>'
            for slide_id in context["slide_ids"]
        ]
        result = assemble_presentation(
            sections,
            technical_contract=context["design_contract"],
            contract_hash=context["design_contract_hash"],
            design_intent=INTENT,
            shared_assets=ASSETS,
        )
        return BoundaryCheckedHtml(result, design_intent=INTENT, shared_assets=ASSETS)


class SampleDeckReuseTests(unittest.TestCase):
    def test_confirmed_sample_design_context_is_reused_for_deck_batches(self):
        with tempfile.TemporaryDirectory() as root:
            builder = IntentBuilder()
            service = TaskService(WorkspaceStore(root), builder=builder)
            service.create("task", "manual")
            service.import_input("task", {"goal": "发布方案", "audience": "评审", "topic": "设计系统", "页数": 3})
            service.generate_narrative("task")
            service.confirm_narrative("task")
            service.generate_outline("task")
            service.confirm_outline("task")
            service.select_samples("task", ["slide-1"])
            sample = service.generate_sample("task")["sample"]
            self.assertEqual(sample["metadata"]["design_intent"], INTENT)
            self.assertEqual(sample["metadata"]["shared_design_assets"], ASSETS)
            confirmation = service.confirm_sample("task")["confirmation"]
            self.assertEqual(confirmation["design_intent"], INTENT)
            deck = service.generate_deck("task")["deck"]
            self.assertEqual(deck["metadata"]["design_intent"], INTENT)
            self.assertEqual(deck["metadata"]["shared_design_assets"], ASSETS)
            self.assertIn(ASSETS["css"], deck["html"])
            deck_calls = [call for call in builder.calls if call["action"] == "deck"]
            self.assertTrue(deck_calls)


class GenerationArchitectureTests(unittest.TestCase):
    def test_generation_path_has_no_builtin_design_vocabulary(self):
        root = Path(__file__).resolve().parents[1]
        source = "\n".join(
            (root / path).read_text(encoding="utf-8")
            for path in (
                "ppt_agent/design_contract.py",
                "ppt_agent/p4.py",
                "ppt_agent/agent_runtime.py",
                "ppt_agent/gateways.py",
                "ppt_agent/generation_preflight.py",
                "ppt_agent/browser_inspection.py",
                "ppt_agent/service.py",
            )
        )
        for token in ("template-registry.json", "_swiss_layout_for_outline", '"S01"', '"S22"', "validate-swiss-deck"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
