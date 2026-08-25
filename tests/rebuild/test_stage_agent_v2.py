from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ppt_agent.errors import ValidationError
from ppt_agent.generation.contracts import HtmlSampleSpec, NarrativeSpec, content_sha256, html_sample_contract_for_assets
from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.generation.stage_agent import StageAgentExecutor
from ppt_agent.model_clients import ModelToolCall, ModelTurn
from ppt_agent.rendering.renderer import DeterministicRenderer
from ppt_agent.rendering.validator import TechnicalValidator
from ppt_agent.skill_runtime import ActiveSkillResolver

from .support import ContractProvider, DESIGN_INTENT, brief, html_slide


class ScriptedAgentClient:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def create(self, **request):
        self.calls.append(request)
        return self.turns.pop(0)


def narrative(thesis: str) -> str:
    return json.dumps({
        "schema_version": "1.0",
        "thesis": thesis,
        "audience_takeaway": "批准下一阶段",
        "story_arc": [
            {"beat_id": "context", "purpose": "建立背景", "message": "说明机会", "evidence_refs": []},
            {"beat_id": "decision", "purpose": "推动决策", "message": "给出路径", "evidence_refs": []},
        ],
        "evidence_refs": [],
        "tone": "清晰",
    }, ensure_ascii=False)


class StageAgentV2Tests(unittest.TestCase):
    def test_stage_agent_records_provider_payload_binding_and_schema_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "skills" / "open"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: open-skill\ndescription: Open narrative skill\n---\nRead the entry before answering.\n",
                encoding="utf-8",
            )
            client = ScriptedAgentClient([
                ModelTurn(None, "entry-response", (ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "entry-call"),)),
                ModelTurn("{", "invalid-json"),
                ModelTurn(narrative("corrected narrative"), "corrected-json"),
            ])
            executor = StageAgentExecutor(
                client,
                ActiveSkillResolver(root / "skills", "open"),
                model="scripted",
                max_steps=4,
                max_provider_calls=4,
            )
            payload = {"context_snapshot_hash": "a" * 64, "original_prompt": {"content": "release input"}}
            result = executor.execute(
                "narrative",
                NarrativeSpec,
                payload=payload,
                idempotency_key="narrative-release-evidence",
                instruction="Return NarrativeSpec.",
            )
            self.assertEqual(result.metadata["provider_input_sha256"], content_sha256(payload))
            self.assertEqual(result.metadata["schema_correction_count"], 1)

    def test_pipeline_uses_skill_agent_and_corrects_before_authority_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "skills" / "open"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: open-skill\ndescription: Open narrative skill\n---\nRead references as needed.\n",
                encoding="utf-8",
            )
            client = ScriptedAgentClient([
                ModelTurn(None, "entry-response", (ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "entry-call"),)),
                ModelTurn(narrative("bad"), "candidate"),
                ModelTurn(narrative("accepted thesis"), "corrected"),
            ])
            executor = StageAgentExecutor(
                client,
                ActiveSkillResolver(root / "skills", "open"),
                model="scripted",
                max_steps=5,
                max_provider_calls=5,
            )
            pipeline = GenerationPipeline(
                ModelGateway(ContractProvider(), model="unused"),
                FileCheckpointStore(root / "checkpoints"),
                DeterministicRenderer(),
                TechnicalValidator(),
                asset_root=root,
                stage_agent=executor,
            )
            validations = []

            def validate(candidate):
                validations.append(candidate.thesis)
                if candidate.thesis == "bad":
                    raise ValidationError("thesis rejected")
                return {"accepted": True}

            result = pipeline.generate_narrative("task", brief(), candidate_validator=validate)
            self.assertEqual(result.value.thesis, "accepted thesis")
            self.assertEqual(validations, ["bad", "accepted thesis"])
            self.assertTrue(result.checkpoint.metadata["skill_entry_read"])
            self.assertIn("SKILL.md", result.checkpoint.metadata["applied_skill_file_hashes"])
            self.assertEqual(result.checkpoint.metadata["semantic_validation"], {"accepted": True})
            self.assertEqual(len(list((root / "checkpoints" / "task" / "authority").glob("*.json"))), 2)

    def test_stage_agent_accepts_html_fragment_contract_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "skills" / "open"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: open-skill\ndescription: Open HTML skill\n---\nDesign the requested pages.\n",
                encoding="utf-8",
            )
            output = {
                "schema_version": "1.0",
                "shared_css": ".slide{background:#0F172A;color:#F8FAFC}",
                "design_intent": DESIGN_INTENT,
                "slides": [html_slide("slide-002"), html_slide("slide-001")],
                "outline_checkpoint_id": "cp-outline",
            }
            client = ScriptedAgentClient([
                ModelTurn(None, "entry-response", (ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "entry-call"),)),
                ModelTurn(json.dumps(output, ensure_ascii=False), "html-candidate"),
            ])
            executor = StageAgentExecutor(
                client,
                ActiveSkillResolver(root / "skills", "open"),
                model="scripted",
                max_steps=4,
                max_provider_calls=4,
            )
            payload = {"slide_ids": ["slide-001", "slide-002"]}
            result = executor.execute(
                "sample",
                html_sample_contract_for_assets((), ("slide-001", "slide-002")),
                payload=payload,
                idempotency_key="html-sample",
                instruction="Return HtmlSampleSpec with html_fragment and shared_css.",
            )
            self.assertIsInstance(result.contract, HtmlSampleSpec)
            self.assertEqual([slide.slide_id for slide in result.contract.slides], ["slide-001", "slide-002"])
            self.assertEqual(result.contract.slides[0].html_fragment, output["slides"][1]["html_fragment"])
            self.assertEqual(client.calls[-1]["response_schema"]["name"], "html_sample_spec_v1")
            self.assertEqual(result.metadata["provider_input_sha256"], content_sha256(payload))
            self.assertEqual(result.metadata["schema_correction_count"], 0)

    def test_sample_stage_rebinds_model_ids_in_page_css_without_correction(self):
        scenarios = {
            "duplicate": ("model-page", "model-page"),
            "deviated": ("model-alpha", "model-beta"),
        }
        for scenario, model_ids in scenarios.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                skill = root / "skills" / "open"
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(
                    "---\nname: open-skill\ndescription: Open HTML skill\n---\nDesign the requested pages.\n",
                    encoding="utf-8",
                )
                slides = []
                for index, model_id in enumerate(model_ids, start=1):
                    slides.append({
                        **html_slide(model_id),
                        "slide_css": (
                            f"#{model_id} #chart-{index}{{color:#22D3EE}}"
                            f'[data-slide-id="{model_id}"] .body{{font-size:20px}}'
                        ),
                    })
                output = {
                    "schema_version": "1.0",
                    "shared_css": ".slide{background:#0F172A;color:#F8FAFC}",
                    "design_intent": DESIGN_INTENT,
                    "slides": slides,
                    "outline_checkpoint_id": "cp-outline",
                }
                client = ScriptedAgentClient([
                    ModelTurn(None, "entry-response", (ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "entry-call"),)),
                    ModelTurn(json.dumps(output, ensure_ascii=False), "html-candidate"),
                ])
                executor = StageAgentExecutor(
                    client,
                    ActiveSkillResolver(root / "skills", "open"),
                    model="scripted",
                    max_steps=4,
                    max_provider_calls=4,
                )
                expected_ids = ("slide-001", "slide-002")

                result = executor.execute(
                    "sample",
                    html_sample_contract_for_assets((), expected_ids),
                    payload={"slide_ids": list(expected_ids)},
                    idempotency_key=f"html-sample-{scenario}",
                    instruction="Return HtmlSampleSpec with html_fragment, slide_css and shared_css.",
                )

                self.assertIsInstance(result.contract, HtmlSampleSpec)
                self.assertEqual([slide.slide_id for slide in result.contract.slides], list(expected_ids))
                self.assertEqual(result.metadata["schema_correction_count"], 0)
                self.assertEqual(len(client.calls), 2)
                for index, (slide, expected_id) in enumerate(zip(result.contract.slides, expected_ids), start=1):
                    self.assertIn(f"#{expected_id} #chart-{index}", slide.slide_css)
                    self.assertIn(f'[data-slide-id="{expected_id}"] .body', slide.slide_css)
                    self.assertIn(f"#chart-{index}", slide.slide_css)
                    self.assertNotIn(model_ids[index - 1], slide.slide_css)


if __name__ == "__main__":
    unittest.main()
