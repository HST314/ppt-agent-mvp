from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ppt_agent.errors import ValidationError
from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.generation.stage_agent import StageAgentExecutor
from ppt_agent.model_clients import ModelToolCall, ModelTurn
from ppt_agent.rendering.renderer import DeterministicRenderer
from ppt_agent.rendering.validator import TechnicalValidator
from ppt_agent.skill_runtime import ActiveSkillResolver

from .support import ContractProvider, brief


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


if __name__ == "__main__":
    unittest.main()
