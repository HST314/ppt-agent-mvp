from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ppt_agent.generation.contracts import HtmlDeckSpec, HtmlSampleSpec
from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.generation.stage_agent import StageAgentExecutor
from ppt_agent.model_clients import ModelToolCall, ModelTurn
from ppt_agent.rendering.validator import TechnicalValidator
from ppt_agent.service import TaskService
from ppt_agent.skill_runtime import ActiveSkillResolver
from ppt_agent.store import WorkspaceStore

from .support import ContractProvider, DESIGN_INTENT, asset_record, brief, html_slide


class ForbiddenDeterministicRenderer:
    version = "forbidden"

    def __init__(self):
        self.calls = 0

    def render(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("agent_html must not invoke DeterministicRenderer.render")


class ForbiddenSampleValidator:
    def validate(self, *_args, **_kwargs):
        raise AssertionError("sample preview must not invoke the technical validator")


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


class ModifyHtmlProvider(ContractProvider):
    def create(self, **request):
        payload = json.loads(request["input"][1]["content"])["input"]
        response = super().create(**request)
        if payload.get("current_stage") != "modify":
            return response
        instruction = payload["modification_instruction"]["instruction"]
        for slide in response.output["slides"]:
            slide["html_fragment"] = slide["html_fragment"].replace(
                "</section>",
                f'<p data-element-id="modify-proof">{instruction}</p></section>',
            )
        if payload["allow_shared_design_change"]:
            response.output["shared_css"] = payload["frozen_shared_css"] + "\n.slide{outline:1px solid #22D3EE}"
            response.output["design_intent"] = {
                **payload["frozen_design_intent"],
                "rationale": payload["frozen_design_intent"]["rationale"] + "；已应用全局修改",
            }
        return response


class AgentHtmlPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = ModifyHtmlProvider()
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
        self.assertIsNone(sample.validation)
        self.assertNotIn("validation", sample.checkpoint.output)

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

        sample = service.modify_sample(
            "html-service",
            "标题更醒目",
            scope="page",
            slide_id=service.sample_view("html-service")["selection"]["slide_ids"][0],
        )["sample"]
        self.assertEqual(sample["metadata"]["generation_core"]["operation"], "modify")
        self.assertEqual(sample["metadata"]["generation_core"]["artifact_kind"], "sample")
        self.assertIn("标题更醒目", sample["html"])

        confirmation = service.confirm_sample("html-service")["confirmation"]
        self.assertEqual(confirmation["generation_core_confirmation"]["contract_name"], "frozen_html_sample_v1")
        deck = service.generate_deck("html-service")["deck"]
        self.assertEqual(deck["metadata"]["generation_core"]["contract_name"], "html_deck_spec_v1")
        self.assertTrue(all(deck["metadata"]["sample_pages_preserved"].values()))
        target_slide_id = list(deck["metadata"]["page_hashes"])[2]
        changed = service.modify_deck(
            "html-service",
            "本页使用更紧凑布局",
            scope="page",
            slide_ids=[target_slide_id],
        )["deck"]
        self.assertEqual(changed["metadata"]["generation_core"]["operation"], "modify")
        self.assertEqual(changed["metadata"]["affected"], [target_slide_id])
        self.assertIn("本页使用更紧凑布局", changed["html"])
        self.assertEqual(self.renderer.calls, 0)

    def test_sample_and_deck_modify_use_context_bound_html_contracts(self):
        narrative = self.pipeline.generate_narrative("modify-task", self.brief)
        outline = self.pipeline.generate_outline("modify-task", self.brief, narrative.checkpoint.checkpoint_id)
        sample = self.pipeline.generate_sample("modify-task", self.brief, outline.checkpoint.checkpoint_id)
        sample_ids = [slide.slide_id for slide in sample.value.slides]
        sample_before = {slide.slide_id: slide.sha256 for slide in sample.value.slides}
        modified_sample = self.pipeline.modify_sample(
            "modify-task",
            self.brief,
            sample.checkpoint.checkpoint_id,
            "突出本页结论",
            scope="page",
            slide_id=sample_ids[0],
            current_fragments={slide.slide_id: slide.html_fragment for slide in sample.value.slides},
        )
        self.assertEqual(modified_sample.checkpoint.stage, "sample")
        self.assertEqual(modified_sample.checkpoint.parent_checkpoint_id, sample.checkpoint.checkpoint_id)
        self.assertEqual(modified_sample.checkpoint.metadata["requested_slide_ids"], [sample_ids[0]])
        modified_sample_by_id = {slide.slide_id: slide for slide in modified_sample.value.slides}
        self.assertNotEqual(modified_sample_by_id[sample_ids[0]].sha256, sample_before[sample_ids[0]])
        for slide_id in sample_ids[1:]:
            self.assertEqual(modified_sample_by_id[slide_id].sha256, sample_before[slide_id])

        confirmation = self.pipeline.confirm_sample("modify-task", modified_sample.checkpoint.checkpoint_id)
        deck = self.pipeline.generate_deck(
            "modify-task",
            self.brief,
            outline.checkpoint.checkpoint_id,
            confirmation.checkpoint.checkpoint_id,
        )
        deck_ids = [slide.slide_id for slide in deck.value.slides]
        target_slide_id = deck_ids[2]
        local = self.pipeline.modify_deck(
            "modify-task",
            self.brief,
            deck.checkpoint.checkpoint_id,
            "压缩本页信息层级",
            scope="page",
            slide_ids=[target_slide_id],
            current_fragments={slide.slide_id: slide.html_fragment for slide in deck.value.slides},
            authoritative_outline={"outline_checkpoint_id": outline.checkpoint.checkpoint_id},
        )
        self.assertEqual(local.checkpoint.metadata["modified_slide_ids"], [target_slide_id])
        self.assertEqual(local.checkpoint.metadata["preserved_slide_ids"], [slide_id for slide_id in deck_ids if slide_id != target_slide_id])
        self.assertEqual(local.value.shared_css, deck.value.shared_css)

        global_change = self.pipeline.modify_deck(
            "modify-task",
            self.brief,
            local.checkpoint.checkpoint_id,
            "统一增加高对比描边",
            scope="global",
            current_fragments={slide.slide_id: slide.html_fragment for slide in local.value.slides},
        )
        self.assertTrue(global_change.checkpoint.metadata["design_system_changed"])
        self.assertEqual(global_change.checkpoint.metadata["modified_slide_ids"], deck_ids)
        self.assertIn("outline:1px solid", global_change.value.shared_css)

        modify_requests = [
            json.loads(request["input"][1]["content"])["input"]
            for request in self.provider.calls
            if json.loads(request["input"][1]["content"])["input"].get("current_stage") == "modify"
        ]
        self.assertEqual([item["artifact_kind"] for item in modify_requests], ["sample", "deck", "deck"])
        for payload in modify_requests:
            self.assertEqual(payload["context_snapshot_hash"], modified_sample.checkpoint.metadata["context_snapshot_hash"])
            self.assertIn("current_artifact", payload)
            self.assertIn("original_prompt", payload)
            self.assertNotIn("content_blocks", json.dumps(payload, ensure_ascii=False))

    def test_global_modify_batches_large_decks_and_freezes_first_design_update(self):
        large_brief = brief(10)
        narrative = self.pipeline.generate_narrative("large-modify", large_brief)
        outline = self.pipeline.generate_outline("large-modify", large_brief, narrative.checkpoint.checkpoint_id)
        sample = self.pipeline.generate_sample("large-modify", large_brief, outline.checkpoint.checkpoint_id)
        confirmation = self.pipeline.confirm_sample("large-modify", sample.checkpoint.checkpoint_id)
        deck = self.pipeline.generate_deck(
            "large-modify",
            large_brief,
            outline.checkpoint.checkpoint_id,
            confirmation.checkpoint.checkpoint_id,
        )
        before = len(self.provider.calls)
        modified = self.pipeline.modify_deck(
            "large-modify",
            large_brief,
            deck.checkpoint.checkpoint_id,
            "统一增加高对比描边",
            scope="global",
            current_fragments={slide.slide_id: slide.html_fragment for slide in deck.value.slides},
        )
        requests = [json.loads(item["input"][1]["content"])["input"] for item in self.provider.calls[before:]]
        self.assertEqual([item["slide_ids"] for item in requests], [
            [f"slide-{index:03d}" for index in range(1, 9)],
            ["slide-009", "slide-010"],
        ])
        self.assertTrue(requests[0]["allow_shared_design_change"])
        self.assertFalse(requests[1]["allow_shared_design_change"])
        self.assertIn("outline:1px solid", requests[1]["frozen_shared_css"])
        self.assertEqual(modified.checkpoint.metadata["modified_slide_ids"], [f"slide-{index:03d}" for index in range(1, 11)])
        self.assertEqual(self.renderer.calls, 0)

    def test_modify_contract_runs_through_production_stage_agent_boundary(self):
        narrative = self.pipeline.generate_narrative("agent-modify", self.brief)
        outline = self.pipeline.generate_outline("agent-modify", self.brief, narrative.checkpoint.checkpoint_id)
        sample = self.pipeline.generate_sample("agent-modify", self.brief, outline.checkpoint.checkpoint_id)
        confirmation = self.pipeline.confirm_sample("agent-modify", sample.checkpoint.checkpoint_id)
        deck = self.pipeline.generate_deck(
            "agent-modify",
            self.brief,
            outline.checkpoint.checkpoint_id,
            confirmation.checkpoint.checkpoint_id,
        )
        target = deck.value.slides[2]
        candidate = {
            "schema_version": "1.0",
            "shared_css": deck.value.shared_css,
            "design_intent": deck.value.design_intent,
            "slides": [{
                **{key: value for key, value in target.to_dict().items() if key != "schema_version"},
                "html_fragment": target.html_fragment.replace(
                    "</section>",
                    '<p data-element-id="modify-proof">Stage Agent 修改</p></section>',
                ),
            }],
        }

        class Client:
            def __init__(self):
                self.turns = [
                    ModelTurn(None, "skill", (ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "read-skill"),)),
                    ModelTurn(json.dumps(candidate, ensure_ascii=False), "candidate"),
                ]

            def create(self, **_request):
                return self.turns.pop(0)

        skill = self.root / "skills" / "open"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: open-skill\ndescription: Modify HTML pages\n---\nApply the requested page edit.\n",
            encoding="utf-8",
        )
        stage_pipeline = GenerationPipeline(
            ModelGateway(self.provider, model="unused"),
            self.pipeline.checkpoints,
            self.renderer,
            TechnicalValidator(),
            asset_root=self.root,
            stage_agent=StageAgentExecutor(
                Client(),
                ActiveSkillResolver(self.root / "skills", "open"),
                model="stage-agent",
                max_steps=4,
                max_provider_calls=4,
            ),
            generation_mode="agent_html",
        )
        modified = stage_pipeline.modify_deck(
            "agent-modify",
            self.brief,
            deck.checkpoint.checkpoint_id,
            "修改第三页",
            scope="page",
            slide_ids=[target.slide_id],
            current_fragments={slide.slide_id: slide.html_fragment for slide in deck.value.slides},
        )
        self.assertEqual(modified.checkpoint.model, "stage-agent")
        self.assertTrue(modified.checkpoint.metadata["skill_entry_read"])
        self.assertIn("SKILL.md", modified.checkpoint.metadata["applied_skill_file_hashes"])
        self.assertEqual(modified.checkpoint.metadata["modified_slide_ids"], [target.slide_id])
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
        self.assertIsNone(sample.validation)

    def test_sample_publishes_preview_directly(self):
        task_brief = brief(3)
        narrative = self.pipeline.generate_narrative("preview-task", task_brief)
        outline = self.pipeline.generate_outline("preview-task", task_brief, narrative.checkpoint.checkpoint_id)
        selected = self.pipeline.select_representative_slides(outline.value)
        candidate = {
            "schema_version": "1.0",
            "shared_css": ".slide{background:#0F172A;color:#F8FAFC}",
            "design_intent": DESIGN_INTENT,
            "slides": [html_slide(slide_id) for slide_id in selected],
            "outline_checkpoint_id": outline.checkpoint.checkpoint_id,
        }

        class Client:
            def __init__(self):
                self.calls = []
                self.turns = [
                    ModelTurn(None, "skill", (ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "read-skill"),)),
                    ModelTurn(json.dumps(candidate, ensure_ascii=False), "candidate"),
                ]

            def create(self, **request):
                self.calls.append(request)
                return self.turns.pop(0)

        client = Client()
        skill = self.root / "skills" / "preview"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: preview-skill\ndescription: Design sample pages\n---\nDesign the requested pages.\n",
            encoding="utf-8",
        )
        pipeline = GenerationPipeline(
            ModelGateway(ContractProvider(), model="unused"),
            self.pipeline.checkpoints,
            self.renderer,
            ForbiddenSampleValidator(),
            asset_root=self.root,
            stage_agent=StageAgentExecutor(
                client,
                ActiveSkillResolver(self.root / "skills", "preview"),
                model="stage-agent",
                max_steps=4,
                max_provider_calls=4,
            ),
            generation_mode="agent_html",
        )

        sample = pipeline.generate_sample("preview-task", task_brief, outline.checkpoint.checkpoint_id)

        self.assertIn('<section class="slide ', sample.artifact.html)
        self.assertIsNone(sample.validation)
        self.assertEqual(sample.checkpoint.metadata["recovery_count"], 0)
        self.assertEqual(sample.checkpoint.metadata["semantic_validation"], {})
        self.assertEqual(len(client.calls), 2)


if __name__ == "__main__":
    unittest.main()
