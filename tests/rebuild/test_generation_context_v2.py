from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ppt_agent.config import ClarificationConfig
from ppt_agent.generation.context import (
    CONTEXT_SECTION_NAMES,
    ContextTextSource,
    GenerationContextV2,
    build_stage_payload,
    stage_payload_metadata,
)
from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.p2 import canonical
from ppt_agent.rendering.renderer import DeterministicRenderer
from ppt_agent.rendering.validator import TechnicalValidator
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

from .support import ContractProvider, brief


def clarification_question(question_id: str, field_path: str, prompt: str) -> dict:
    return {
        "question_id": question_id,
        "field_path": field_path,
        "prompt": prompt,
        "helper_text": "请保留用户原始回答并采用确认后的值。",
        "options": [{"value": "default", "label": "默认", "description": "使用默认值"}],
        "allow_other": True,
        "blocking": True,
    }


class SequentialClarifier:
    model = "clarifier-context-test"

    def __init__(self, rounds):
        self.rounds = list(rounds)

    def clarify(self, _payload):
        return {"questions": self.rounds.pop(0), "model": self.model}


class GenerationContextContractTests(unittest.TestCase):
    def test_context_is_content_addressed_deeply_immutable_and_payload_is_stable(self):
        task_brief = brief(4)
        context = GenerationContextV2.from_task_brief(task_brief)
        parsed = GenerationContextV2.parse(context.to_dict())
        self.assertEqual(parsed.context_hash, context.context_hash)
        self.assertEqual(parsed.section_names, CONTEXT_SECTION_NAMES)
        with self.assertRaises(TypeError):
            parsed.normalized_task_card["goal"] = "mutated"

        payload = build_stage_payload(parsed, "narrative", {"task_brief": task_brief.to_dict()})
        metadata = stage_payload_metadata(parsed, payload)
        self.assertEqual(payload["original_prompt"]["content_hash"], parsed.original_prompt.content_hash)
        self.assertEqual(tuple(payload["context_sections"]), CONTEXT_SECTION_NAMES)
        self.assertEqual(metadata["context_snapshot_hash"], parsed.context_hash)
        self.assertEqual(len(metadata["stage_payload_hash"]), 64)


class GenerationContextAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = ContractProvider()
        self.checkpoints = FileCheckpointStore(self.root / "checkpoints")
        self.pipeline = GenerationPipeline(
            ModelGateway(self.provider, model="context-provider"),
            self.checkpoints,
            DeterministicRenderer(),
            TechnicalValidator(),
            asset_root=self.root,
            batch_size=2,
        )
        clarifier = SequentialClarifier([
            [clarification_question("q-source", "data_source", "请提供完整背景资料")],
            [clarification_question("q-style", "style_direction", "请确认表达风格")],
        ])
        self.service = TaskService(
            WorkspaceStore(self.root / "tasks"),
            generation_pipeline=self.pipeline,
            clarifier=clarifier,
            clarification_config=ClarificationConfig(max_questions_per_round=2, max_rounds=2, style="comprehensive"),
        )
        self.service.create("context-task", "manual")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _answer(text: str) -> dict:
        return {"option": "Other", "other": text}

    def _finish_clarification(self):
        source = {
            "goal": "形成建设共识",
            "audience": "学院管理层",
            "topic": "学院建设方案",
            "constraints": {"page_count": 6},
        }
        self.service.import_input("context-task", source)
        self.service.generate_clarification("context-task")
        self.service.answer_clarification("context-task", "q-source", self._answer("完整资料：学科、平台、培养与产教融合均需展开。"))
        self.service.generate_clarification("context-task")
        self.service.answer_clarification("context-task", "q-style", self._answer("严谨、现代、信息清晰"))
        return source

    def test_all_stage_requests_and_checkpoints_share_full_context(self):
        source = self._finish_clarification()
        persisted_contexts = self.service.versions("context-task", "generation-context")
        self.assertEqual(len(persisted_contexts), 1)
        self.assertTrue(persisted_contexts[0]["metadata"]["immutable"])
        context_before = self.service.generation_context_view("context-task")
        self.assertEqual(persisted_contexts[0]["hash"], context_before["context_snapshot_hash"])
        transcript = context_before["clarification_transcript"]
        self.assertEqual([item["round"] for item in transcript], [1, 2])
        self.assertEqual(transcript[0]["exchanges"][0]["raw_answer"], self._answer("完整资料：学科、平台、培养与产教融合均需展开。"))
        self.assertEqual(transcript[0]["exchanges"][0]["adopted_value"], "完整资料：学科、平台、培养与产教融合均需展开。")

        task_brief = self.service._generation_core_brief("context-task")
        resources = {item.resource_id: item.content for item in task_brief.text_resources}
        self.assertEqual(resources["original-prompt"], canonical(source).decode("utf-8"))
        self.assertIn("q-source", resources["clarification-transcript"])
        self.assertIn("完整资料：学科、平台、培养与产教融合均需展开。", resources["confirmed-task-card"])
        self.assertTrue(any("产教融合" in content for key, content in resources.items() if key.startswith("source-material-")))

        narrative = self.service.generate_narrative("context-task")["narrative"]
        self.service.confirm_narrative("context-task")
        outline = self.service.generate_outline("context-task")["outline"]
        self.service.confirm_outline("context-task")
        sample = self.service.generate_sample("context-task")["sample"]
        confirmation = self.service.confirm_sample("context-task")["confirmation"]
        deck = self.service.generate_deck("context-task")["deck"]

        expected_hash = context_before["context_snapshot_hash"]
        checkpoint_ids = [
            narrative["metadata"]["generation_core"]["checkpoint_id"],
            outline["metadata"]["generation_core"]["checkpoint_id"],
            sample["metadata"]["generation_core"]["checkpoint_id"],
            confirmation["generation_core_confirmation"]["checkpoint_id"],
            deck["metadata"]["generation_core"]["checkpoint_id"],
        ]
        for checkpoint_id in checkpoint_ids:
            checkpoint = self.checkpoints.load(checkpoint_id)
            self.assertEqual(checkpoint.metadata["context_snapshot_hash"], expected_hash)
            self.assertEqual(len(checkpoint.metadata["stage_payload_hash"]), 64)
        for checkpoint in self.checkpoints.chain(checkpoint_ids[-1]):
            self.assertEqual(checkpoint.metadata["context_snapshot_hash"], expected_hash)

        for request in self.provider.calls:
            payload = json.loads(request["input"][1]["content"])["input"]
            self.assertEqual(payload["context_snapshot_hash"], expected_hash)
            self.assertEqual(payload["original_prompt"]["content"], canonical(source).decode("utf-8"))
            self.assertEqual(len(payload["clarification_transcript"]), 2)
            self.assertEqual(set(payload["context_sections"]), set(CONTEXT_SECTION_NAMES))

        calls_before_change = len(self.provider.calls)
        changed = self.service.answer_clarification("context-task", "q-source", self._answer("更新资料：重点转为人才培养与联合实验室。"))
        self.assertIn("narrative", changed["invalidated"])
        self.assertEqual(changed["state"]["stage"], "clarification")
        self.assertIsNone(self.service._current_version("context-task", "narrative"))
        self.assertIsNone(self.service._current_version("context-task", "outline"))
        self.assertIsNone(self.service._current_version("context-task", "sample"))
        self.assertIsNone(self.service._current_version("context-task", "deck"))

        context_after = self.service.generation_context_view("context-task")
        self.assertNotEqual(context_after["context_snapshot_hash"], expected_hash)
        self.assertEqual(context_after["clarification_transcript"][0]["exchanges"][0]["adopted_value"], "更新资料：重点转为人才培养与联合实验室。")
        regenerated = self.service.generate_narrative("context-task")["narrative"]
        self.assertNotEqual(regenerated["metadata"]["generation_core"]["checkpoint_id"], checkpoint_ids[0])
        self.assertEqual(regenerated["metadata"]["generation_core"]["context_snapshot_hash"], context_after["context_snapshot_hash"])
        self.assertGreater(len(self.provider.calls), calls_before_change)


if __name__ == "__main__":
    unittest.main()
