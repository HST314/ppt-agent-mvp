from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ppt_agent.browser_inspection import ChromiumDeckInspector
from ppt_agent.generation.bootstrap import resolve_chromium_executable
from ppt_agent.generation.contracts import DeckSpec
from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.rendering.renderer import DeterministicRenderer
from ppt_agent.rendering.validator import TechnicalValidator
from tests.rebuild.support import ContractProvider, THEME, brief


class RebuildGoldenPathBrowserTests(unittest.TestCase):
    def test_contract_capacity_extremes_stay_inside_canvas(self):
        chromium = resolve_chromium_executable(Path(__file__).resolve().parents[2])
        validator = TechnicalValidator(ChromiumDeckInspector(executable_path=chromium), require_browser=True)
        theme = {**THEME, "space_unit": 20}
        slides = [
            {
                "slide_id": "slide-001", "role": "cover", "title": "标题" * 36,
                "content_blocks": [
                    {"type": "paragraph", "block_id": f"cover-{index}", "text": "核心信息" * 17}
                    for index in range(4)
                ],
                "layout_family": "cover", "asset_refs": [], "speaker_notes": "",
            },
            {
                "slide_id": "slide-002", "role": "analysis", "title": "分析" * 24,
                "content_blocks": [
                    {"type": "paragraph", "block_id": f"column-{index}", "text": "结构化内容" * 16}
                    for index in range(4)
                ],
                "layout_family": "columns", "asset_refs": [], "speaker_notes": "",
            },
            {
                "slide_id": "slide-003", "role": "data", "title": "指标" * 24,
                "content_blocks": [
                    {"type": "metric", "block_id": f"metric-{index}", "label": "关键指标" * 8, "value": "100%" * 6}
                    for index in range(3)
                ],
                "layout_family": "metrics", "asset_refs": [], "speaker_notes": "",
            },
            {
                "slide_id": "slide-004", "role": "table", "title": "对比" * 24,
                "content_blocks": [{
                    "type": "table", "block_id": "comparison", "rows": [["单元格内容" * 2 for _ in range(4)] for _ in range(6)],
                }],
                "layout_family": "table", "asset_refs": [], "speaker_notes": "",
            },
            {
                "slide_id": "slide-005", "role": "closing", "title": "结论" * 24,
                "content_blocks": [{"type": "quote", "block_id": "closing-quote", "text": "结论信息" * 35, "attribution": "评审委员会" * 8}],
                "layout_family": "quote", "asset_refs": [], "speaker_notes": "",
            },
        ]
        deck = DeckSpec.parse({
            "schema_version": "1.0", "slides": slides, "theme_tokens": theme, "shared_assets": [],
            "outline_checkpoint_id": "cp-outline", "sample_checkpoint_id": "cp-sample",
        })
        artifact = DeterministicRenderer().render(deck)
        report = validator.validate(artifact.html, [item["slide_id"] for item in slides])
        self.assertTrue(report.passed)

    def test_fresh_task_reaches_a_browser_validated_deck_once(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            provider = ContractProvider()
            chromium = resolve_chromium_executable(Path(__file__).resolve().parents[2])
            validator = TechnicalValidator(ChromiumDeckInspector(executable_path=chromium), require_browser=True)
            pipeline = GenerationPipeline(ModelGateway(provider, model="test"), FileCheckpointStore(root / "cp"), DeterministicRenderer(), validator, asset_root=root)
            task_brief = brief(4)
            narrative = pipeline.generate_narrative("browser-task", task_brief)
            outline = pipeline.generate_outline("browser-task", task_brief, narrative.checkpoint.checkpoint_id)
            sample = pipeline.generate_sample("browser-task", task_brief, outline.checkpoint.checkpoint_id)
            confirmation = pipeline.confirm_sample("browser-task", sample.checkpoint.checkpoint_id)
            deck = pipeline.generate_deck("browser-task", task_brief, outline.checkpoint.checkpoint_id, confirmation.checkpoint.checkpoint_id)
            self.assertTrue(sample.validation.browser["available"])
            self.assertTrue(deck.validation.browser["passed"])
            self.assertEqual(len(deck.value.slides), 4)
            calls = len(provider.calls)
            repeated = pipeline.generate_deck("browser-task", task_brief, outline.checkpoint.checkpoint_id, confirmation.checkpoint.checkpoint_id)
            self.assertTrue(repeated.reused)
            self.assertEqual(len(provider.calls), calls)


if __name__ == "__main__":
    unittest.main()
