from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.rendering.renderer import DeterministicRenderer
from ppt_agent.rendering.validator import TechnicalValidator
from tests.rebuild.support import ContractProvider, brief


class RebuildOfflineDeliveryTests(unittest.TestCase):
    def test_offline_delivery_is_bound_to_deck_checkpoint_and_hashes(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            pipeline = GenerationPipeline(ModelGateway(ContractProvider(), model="test"), FileCheckpointStore(root / "cp"), DeterministicRenderer(), TechnicalValidator(), asset_root=root)
            task_brief = brief(4)
            narrative = pipeline.generate_narrative("offline-task", task_brief)
            outline = pipeline.generate_outline("offline-task", task_brief, narrative.checkpoint.checkpoint_id)
            sample = pipeline.generate_sample("offline-task", task_brief, outline.checkpoint.checkpoint_id)
            confirmation = pipeline.confirm_sample("offline-task", sample.checkpoint.checkpoint_id)
            deck = pipeline.generate_deck("offline-task", task_brief, outline.checkpoint.checkpoint_id, confirmation.checkpoint.checkpoint_id)
            delivery = pipeline.publish_offline("offline-task", deck.checkpoint.checkpoint_id, root / "delivery")
            manifest = json.loads((root / "delivery" / "manifest.json").read_text())
            self.assertEqual(manifest["deck_checkpoint_id"], deck.checkpoint.checkpoint_id)
            self.assertEqual(hashlib.sha256((root / "delivery" / "index.html").read_bytes()).hexdigest(), manifest["files"]["index.html"])
            self.assertNotIn("http://", (root / "delivery" / "index.html").read_text())
            self.assertEqual(delivery.value["manifest_sha256"], hashlib.sha256((root / "delivery" / "manifest.json").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
