from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ppt_agent.generation.contracts import HtmlDeckSpec, HtmlSampleSpec
from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.rendering.validator import TechnicalValidator
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

from .support import ContractProvider, asset_record, brief


class ForbiddenDeterministicRenderer:
    version = "forbidden"

    def __init__(self):
        self.calls = 0

    def render(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("agent_html must not invoke DeterministicRenderer.render")


class AssetHtmlProvider(ContractProvider):
    def create(self, **request):
        response = super().create(**request)
        if request["response_schema"]["name"] == "html_sample_spec_v1":
            first = response.output["slides"][0]
            first["html_fragment"] = first["html_fragment"].replace(
                "</section>",
                '<img src="resources://image-1" alt="已确认素材"></section>',
            )
            first["asset_refs"] = ["image-1"]
        return response


class AgentHtmlPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = ContractProvider()
        self.renderer = ForbiddenDeterministicRenderer()
        self.pipeline = GenerationPipeline(
            ModelGateway(self.provider, model="html-provider"),
            FileCheckpointStore(self.root / "checkpoints"),
            self.renderer,
            TechnicalValidator(),
            asset_root=self.root,
            batch_size=2,
            generation_mode="agent_html",
        )
        self.brief = brief(6)

    def tearDown(self):
        self.temp.cleanup()

    def test_agent_html_is_the_rendered_sample_and_deck_path(self):
        narrative = self.pipeline.generate_narrative("html-task", self.brief)
        outline = self.pipeline.generate_outline("html-task", self.brief, narrative.checkpoint.checkpoint_id)
        sample = self.pipeline.generate_sample("html-task", self.brief, outline.checkpoint.checkpoint_id)

        self.assertIsInstance(sample.value, HtmlSampleSpec)
        self.assertEqual(sample.checkpoint.contract_name, "html_sample_spec_v1")
        self.assertEqual(sample.checkpoint.metadata["generation_mode"], "agent_html")
        self.assertIn("editorial", sample.artifact.html)
        self.assertIn(sample.value.shared_css, sample.artifact.html)
        self.assertNotIn("content_blocks", sample.checkpoint.output)

        confirmation = self.pipeline.confirm_sample("html-task", sample.checkpoint.checkpoint_id)
        self.assertEqual(confirmation.checkpoint.contract_name, "frozen_html_sample_v1")
        self.assertNotIn("layout_families", confirmation.value)
        self.assertEqual(confirmation.value["slides"], [slide.to_dict() for slide in sample.value.slides])

        deck = self.pipeline.generate_deck(
            "html-task",
            self.brief,
            outline.checkpoint.checkpoint_id,
            confirmation.checkpoint.checkpoint_id,
        )
        self.assertIsInstance(deck.value, HtmlDeckSpec)
        self.assertEqual(deck.checkpoint.contract_name, "html_deck_spec_v1")
        self.assertTrue(deck.validation.passed)
        self.assertEqual(self.renderer.calls, 0)
        self.assertEqual(deck.value.shared_css, sample.value.shared_css)
        self.assertEqual(deck.value.design_intent, sample.value.design_intent)

        deck_by_id = {slide.slide_id: slide for slide in deck.value.slides}
        for slide in sample.value.slides:
            self.assertEqual(deck_by_id[slide.slide_id].html_fragment, slide.html_fragment)
            self.assertEqual(deck_by_id[slide.slide_id].slide_css, slide.slide_css)

        review = self.pipeline.create_review_input("html-task", deck.checkpoint.checkpoint_id)
        self.assertEqual(len(review.value["slides"]), self.brief.slide_count)
        self.assertEqual(review.value["deck_sha256"], deck.artifact.sha256)

        sample_request = next(
            request for request in self.provider.calls if request["response_schema"]["name"] == "html_sample_spec_v1"
        )
        sample_payload = json.loads(sample_request["input"][1]["content"])["input"]
        self.assertEqual(sample_payload["generation_mode"], "agent_html")
        self.assertEqual(sample_payload["narrative"], narrative.value.to_dict())
        self.assertEqual(sample_payload["context_snapshot_hash"], sample.checkpoint.metadata["context_snapshot_hash"])

        deck_requests = [
            request for request in self.provider.calls if request["response_schema"]["name"] == "html_deck_batch_spec_v1"
        ]
        self.assertTrue(deck_requests)
        for request in deck_requests:
            payload = json.loads(request["input"][1]["content"])["input"]
            self.assertEqual(payload["frozen_shared_css"], sample.value.shared_css)
            self.assertEqual(payload["frozen_design_intent"], sample.value.design_intent)
            self.assertEqual(payload["confirmed_sample"]["slides"], confirmation.value["slides"])

    def test_task_service_persists_agent_design_instead_of_theme_projection(self):
        service = TaskService(WorkspaceStore(self.root / "tasks"), generation_pipeline=self.pipeline)
        service.create("html-service", "manual")
        service.import_input(
            "html-service",
            {"goal": "批准投入", "audience": "管理团队", "topic": "增长计划", "页数": 6},
        )
        service.generate_narrative("html-service")
        service.confirm_narrative("html-service")
        service.generate_outline("html-service")
        service.confirm_outline("html-service")
        sample = service.generate_sample("html-service")["sample"]
        self.assertEqual(sample["metadata"]["generation_core"]["contract_name"], "html_sample_spec_v1")
        self.assertEqual(sample["metadata"]["generation_core"]["generation_mode"], "agent_html")
        self.assertEqual(sample["metadata"]["design_intent"]["style_summary"], "高对比编辑式演示")
        self.assertIn("background:#0F172A", sample["metadata"]["shared_design_assets"]["css"])

        confirmation = service.confirm_sample("html-service")["confirmation"]
        self.assertEqual(confirmation["generation_core_confirmation"]["contract_name"], "frozen_html_sample_v1")
        deck = service.generate_deck("html-service")["deck"]
        self.assertEqual(deck["metadata"]["generation_core"]["contract_name"], "html_deck_spec_v1")
        self.assertTrue(all(deck["metadata"]["sample_pages_preserved"].values()))
        self.assertEqual(self.renderer.calls, 0)

    def test_declared_resources_are_rewritten_to_verified_offline_closure(self):
        provider = AssetHtmlProvider()
        pipeline = GenerationPipeline(
            ModelGateway(provider, model="html-provider"),
            FileCheckpointStore(self.root / "asset-checkpoints"),
            self.renderer,
            TechnicalValidator(),
            asset_root=self.root,
            generation_mode="agent_html",
        )
        task_brief = brief(3, [asset_record(self.root)])
        narrative = pipeline.generate_narrative("asset-task", task_brief)
        outline = pipeline.generate_outline("asset-task", task_brief, narrative.checkpoint.checkpoint_id)
        sample = pipeline.generate_sample("asset-task", task_brief, outline.checkpoint.checkpoint_id)
        expected_path = f"assets/{task_brief.resource_manifest[0].content_hash}.png"
        self.assertNotIn("resources://", sample.artifact.html)
        self.assertIn(expected_path, sample.artifact.html)
        self.assertEqual(sample.validation.asset_paths, (expected_path,))


if __name__ == "__main__":
    unittest.main()
