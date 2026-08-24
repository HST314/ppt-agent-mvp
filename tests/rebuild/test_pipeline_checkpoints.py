from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ppt_agent.generation.errors import CheckpointConflict
from ppt_agent.errors import ValidationError
from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.rendering.renderer import DeterministicRenderer
from ppt_agent.rendering.validator import TechnicalValidator

from .support import ContractProvider, brief


class PipelineCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = ContractProvider()
        self.store = FileCheckpointStore(self.root / "checkpoints")
        self.pipeline = GenerationPipeline(
            ModelGateway(self.provider, model="test-model"),
            self.store,
            DeterministicRenderer(),
            TechnicalValidator(),
            asset_root=self.root,
            batch_size=2,
        )
        self.brief = brief(6)

    def tearDown(self):
        self.temp.cleanup()

    def golden_path(self):
        narrative = self.pipeline.generate_narrative("task-1", self.brief)
        outline = self.pipeline.generate_outline("task-1", self.brief, narrative.checkpoint.checkpoint_id)
        sample = self.pipeline.generate_sample("task-1", self.brief, outline.checkpoint.checkpoint_id)
        confirmation = self.pipeline.confirm_sample("task-1", sample.checkpoint.checkpoint_id)
        deck = self.pipeline.generate_deck("task-1", self.brief, outline.checkpoint.checkpoint_id, confirmation.checkpoint.checkpoint_id)
        return narrative, outline, sample, confirmation, deck

    def test_complete_chain_is_parent_bound_and_sample_pages_are_immutable(self):
        narrative, outline, sample, confirmation, deck = self.golden_path()
        chain = self.store.chain(deck.checkpoint.checkpoint_id)
        self.assertEqual([item.stage for item in chain], ["deck", "sample_confirmed", "sample", "outline", "narrative", "brief"])
        deck_by_id = {item.slide_id: item.sha256 for item in deck.value.slides}
        for sample_slide in sample.value.slides:
            self.assertEqual(deck_by_id[sample_slide.slide_id], sample_slide.sha256)
        self.assertEqual([slide.slide_id for slide in deck.value.slides], [f"slide-{index:03d}" for index in range(1, 7)])
        self.assertTrue(deck.validation.passed)

    def test_stage_replay_reads_same_checkpoint_without_provider_call(self):
        first = self.pipeline.generate_narrative("task-1", self.brief)
        calls = len(self.provider.calls)
        second = self.pipeline.generate_narrative("task-1", self.brief)
        self.assertTrue(second.reused)
        self.assertEqual(first.checkpoint.checkpoint_id, second.checkpoint.checkpoint_id)
        self.assertEqual(len(self.provider.calls), calls)

    def test_outline_role_is_server_projected_into_sample(self):
        original_create = self.provider.create

        def changed_role(**request):
            response = original_create(**request)
            if request["response_schema"]["name"] == "sample_spec_v1":
                response.output["slides"][0]["role"] = "模型改写角色"
                response.output["slides"][0]["slide_id"] = "model-page"
                response.output["outline_checkpoint_id"] = "model-checkpoint"
            return response

        self.provider.create = changed_role
        narrative = self.pipeline.generate_narrative("task-role", self.brief)
        outline = self.pipeline.generate_outline("task-role", self.brief, narrative.checkpoint.checkpoint_id)
        sample = self.pipeline.generate_sample("task-role", self.brief, outline.checkpoint.checkpoint_id)
        roles = {slide.slide_id: slide.role for slide in outline.value.slides}
        self.assertEqual(sample.value.outline_checkpoint_id, outline.checkpoint.checkpoint_id)
        self.assertIn(sample.value.slides[0].slide_id, roles)
        self.assertEqual(sample.value.slides[0].role, roles[sample.value.slides[0].slide_id])

    def test_checkpoint_tampering_is_detected(self):
        result = self.pipeline.generate_narrative("task-1", self.brief)
        path = next((self.root / "checkpoints").glob(f"*/checkpoints/{result.checkpoint.checkpoint_id}.json"))
        value = json.loads(path.read_text())
        value["output"]["thesis"] = "tampered"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(CheckpointConflict):
            self.store.load(result.checkpoint.checkpoint_id)

    def test_business_rejection_never_creates_or_reuses_authority(self):
        def reject(_candidate):
            raise ValidationError("evidence rejected")

        with self.assertRaises(ValidationError):
            self.pipeline.generate_narrative("task-rejected", self.brief, candidate_validator=reject)
        calls_after_first = len(self.provider.calls)
        task_root = self.root / "checkpoints" / "task-rejected"
        self.assertEqual(len(list((task_root / "authority").glob("*.json"))), 1)
        self.assertFalse(any(json.loads(path.read_text())["stage"] == "narrative" for path in (task_root / "checkpoints").glob("*.json")))
        rejected = list((task_root / "rejected").glob("*.json"))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(json.loads(rejected[0].read_text())["status"], "rejected")

        with self.assertRaises(ValidationError):
            self.pipeline.generate_narrative("task-rejected", self.brief, candidate_validator=reject)
        self.assertGreater(len(self.provider.calls), calls_after_first)


if __name__ == "__main__":
    unittest.main()
