from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.rendering.renderer import DeterministicRenderer
from ppt_agent.rendering.validator import TechnicalValidator

from .support import ContractProvider, brief


class GenerationIdempotencyTests(unittest.TestCase):
    def test_concurrent_same_stage_creates_one_provider_result_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            provider = ContractProvider()
            pipeline = GenerationPipeline(ModelGateway(provider, model="test"), FileCheckpointStore(root / "cp"), DeterministicRenderer(), TechnicalValidator(), asset_root=root)
            results = []
            failures = []

            def run():
                try:
                    results.append(pipeline.generate_narrative("task-concurrent", brief()))
                except Exception as exc:  # pragma: no cover - assertion below exposes it
                    failures.append(exc)

            threads = [threading.Thread(target=run) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(failures, [])
            self.assertEqual(len({item.checkpoint.checkpoint_id for item in results}), 1)
            self.assertEqual(len(provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
