from __future__ import annotations

import copy
import unittest

from ppt_agent.generation.contracts import DeckSpec, HtmlSlideSpec, NarrativeSpec, OutlineDraft, OutlineSpec, SampleSpec, SlideSpec, TaskBrief, ThemeTokens, html_sample_contract_for_assets, narrative_contract_for_evidence, outline_contract_for_evidence, sample_contract_for_assets, slide_batch_contract_for_assets
from ppt_agent.generation.errors import ContractValidationError

from .support import THEME, brief, slide


class ContractTests(unittest.TestCase):
    def test_business_role_accepts_localized_text(self):
        draft = OutlineDraft.parse({"schema_version": "1.0", "slides": [
            {"role": "方案总览", "title": "总览", "message": "说明方案", "evidence_refs": [], "visual_intent": "清晰层级"},
        ]})
        value = slide("slide-001", "方案总览")
        self.assertEqual(draft.slides[0].role, "方案总览")
        self.assertEqual(SlideSpec.parse(value).role, "方案总览")

    def test_task_brief_is_strict_versioned_and_round_trips(self):
        value = brief()
        self.assertEqual(TaskBrief.parse(value.to_dict()), value)
        self.assertEqual(TaskBrief.provider_schema()["strict"], True)
        self.assertNotIn("uniqueItems", str(TaskBrief.provider_schema()["schema"]))
        invalid = value.to_dict() | {"workflow_state": "delivery"}
        with self.assertRaises(ContractValidationError):
            TaskBrief.parse(invalid)

    def test_outline_ids_are_server_assigned_and_count_is_exact(self):
        draft = OutlineDraft.parse({"schema_version": "1.0", "slides": [
            {"role": "cover", "title": "开始", "message": "背景", "evidence_refs": [], "visual_intent": "封面"},
            {"role": "closing", "title": "结论", "message": "行动", "evidence_refs": [], "visual_intent": "收束"},
        ]})
        outline = OutlineSpec.from_draft(draft, expected_slide_count=2)
        self.assertEqual([item.slide_id for item in outline.slides], ["slide-001", "slide-002"])
        with self.assertRaises(ContractValidationError):
            OutlineSpec.from_draft(draft, expected_slide_count=3)

    def test_content_blocks_are_a_finite_strict_union(self):
        value = slide("slide-001", "cover", layout="cover")
        parsed = SlideSpec.parse(value)
        self.assertEqual(parsed.content_blocks[0].type, "paragraph")
        invalid = copy.deepcopy(value)
        invalid["content_blocks"][0]["type"] = "html"
        with self.assertRaises(ContractValidationError):
            SlideSpec.parse(invalid)
        invalid = copy.deepcopy(value)
        invalid["content_blocks"][0]["style"] = "position:fixed"
        with self.assertRaises(ContractValidationError):
            SlideSpec.parse(invalid)

    def test_slide_contract_enforces_canvas_capacity(self):
        value = slide("slide-001", "analysis", layout="columns")
        value["content_blocks"] = [
            {"type": "paragraph", "block_id": f"body-{index}", "text": "内容" * 45}
            for index in range(4)
        ]
        self.assertLessEqual(sum(len(item["text"]) for item in value["content_blocks"]), 420)
        self.assertEqual(len(SlideSpec.parse(value).content_blocks), 4)

        too_many = copy.deepcopy(value)
        too_many["content_blocks"].append({"type": "paragraph", "block_id": "body-extra", "text": "超出"})
        with self.assertRaises(ContractValidationError):
            SlideSpec.parse(too_many)

        too_dense = copy.deepcopy(value)
        too_dense["title"] = "标题" * 36
        with self.assertRaises(ContractValidationError):
            SlideSpec.parse(too_dense)

        theme = copy.deepcopy(THEME)
        theme["space_unit"] = 21
        with self.assertRaises(ContractValidationError):
            ThemeTokens.parse(theme)

    def test_provider_sample_schema_is_bound_to_frozen_assets(self):
        without_assets = sample_contract_for_assets((), 2).provider_schema()["schema"]
        variants = without_assets["properties"]["slides"]["items"]["properties"]["content_blocks"]["items"]["anyOf"]
        self.assertNotIn("image", {item["properties"]["type"].get("const") for item in variants})
        self.assertEqual(without_assets["properties"]["shared_assets"]["maxItems"], 0)
        self.assertEqual(without_assets["properties"]["slides"]["minItems"], 2)
        with_assets = sample_contract_for_assets(("asset-one",), 3).provider_schema()["schema"]
        self.assertEqual(with_assets["properties"]["shared_assets"]["items"]["enum"], ["asset-one"])

    def test_provider_schemas_bind_evidence_batch_size_and_layouts(self):
        narrative = narrative_contract_for_evidence(()).provider_schema()["schema"]
        self.assertEqual(narrative["properties"]["evidence_refs"]["maxItems"], 0)
        outline = outline_contract_for_evidence(("fact-one",), 4).provider_schema()["schema"]
        self.assertEqual(outline["properties"]["slides"]["minItems"], 4)
        self.assertEqual(outline["properties"]["slides"]["items"]["properties"]["evidence_refs"]["items"]["enum"], ["fact-one"])
        batch = slide_batch_contract_for_assets((), 2, ("metrics",)).provider_schema()["schema"]
        self.assertEqual(batch["properties"]["slides"]["maxItems"], 2)
        self.assertEqual(batch["properties"]["slides"]["items"]["properties"]["layout_family"]["enum"], ["metrics"])
        slide_schema = batch["properties"]["slides"]["items"]
        self.assertEqual(slide_schema["properties"]["title"]["maxLength"], 72)
        self.assertEqual(slide_schema["properties"]["content_blocks"]["maxItems"], 4)

    def test_deck_enforces_order_theme_layout_and_resource_closure(self):
        slides = [slide("slide-001", "cover", layout="cover"), slide("slide-002", "data", layout="metrics")]
        value = {
            "schema_version": "1.0", "slides": slides, "theme_tokens": THEME, "shared_assets": [],
            "outline_checkpoint_id": "cp-outline", "sample_checkpoint_id": "cp-sample",
        }
        theme = ThemeTokens.parse(THEME)
        deck = DeckSpec.parse(value, expected_slide_ids=["slide-001", "slide-002"], frozen_theme=theme, allowed_layouts={"cover", "metrics"})
        self.assertEqual(deck.theme_tokens, theme)
        with self.assertRaises(ContractValidationError):
            DeckSpec.parse(value, expected_slide_ids=["slide-002", "slide-001"])

    def test_provider_and_local_contract_both_require_version(self):
        self.assertIn("schema_version", NarrativeSpec.provider_schema()["schema"]["required"])
        with self.assertRaises(ContractValidationError):
            SampleSpec.parse({"schema_version": "2.0"})

    def test_agent_html_contract_binds_page_ids_assets_and_page_css(self):
        schema = html_sample_contract_for_assets(("asset-one",), ("slide-001", "slide-002")).provider_schema()["schema"]
        self.assertEqual(schema["properties"]["slides"]["minItems"], 2)
        self.assertEqual(schema["properties"]["slides"]["items"]["properties"]["slide_id"]["enum"], ["slide-001", "slide-002"])
        self.assertEqual(schema["properties"]["slides"]["items"]["properties"]["asset_refs"]["items"]["enum"], ["asset-one"])

        valid = {
            "schema_version": "1.0",
            "slide_id": "slide-001",
            "html_fragment": '<section class="slide custom" id="slide-001" data-slide-id="slide-001"><section><p>内容</p></section></section>',
            "slide_css": "#slide-001 p{font-size:24px}",
            "asset_refs": [],
            "speaker_notes": "",
        }
        self.assertEqual(HtmlSlideSpec.parse(valid).slide_id, "slide-001")
        with self.assertRaises(ContractValidationError):
            HtmlSlideSpec.parse(valid | {"html_fragment": valid["html_fragment"] + '<section class="extra"></section>'})
        with self.assertRaises(ContractValidationError):
            HtmlSlideSpec.parse(valid | {"slide_css": ".slide{font-size:12px}"})


if __name__ == "__main__":
    unittest.main()
