from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ppt_agent.generation.bootstrap import ResponsesProviderAdapter
from ppt_agent.generation.model_gateway import ModelGateway
from ppt_agent.generation.pipeline import FileCheckpointStore, GenerationPipeline
from ppt_agent.rendering.renderer import DeterministicRenderer
from ppt_agent.rendering.validator import TechnicalValidator
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from tests.rebuild.support import ContractProvider


class StubPipeline:
    def __init__(self, ready=True):
        self.ready = ready
        self.calls = 0

    def preflight(self):
        self.calls += 1
        return {"ready": self.ready, "pipeline_version": "1.0.0", "checks": {"chromium": {"ready": self.ready}}}


class StructuredProbeClient:
    def __init__(self, text='{"ready":true}'):
        self.text = text
        self.config = SimpleNamespace(request_timeout_seconds=60)
        self.calls = []

    def create(self, **request):
        self.calls.append(request)
        return SimpleNamespace(text=self.text)


class GenerationCoreServiceAdapterTests(unittest.TestCase):
    def test_provider_preflight_proves_strict_schema_support(self):
        client = StructuredProbeClient()
        result = ResponsesProviderAdapter(client).probe_capabilities()
        self.assertEqual(result, {"basic_response": True, "json_schema": True})
        self.assertTrue(client.calls[0]["response_schema"]["strict"])
        self.assertEqual(client.calls[0]["response_schema"]["schema"]["additionalProperties"], False)

    def test_provider_preflight_rejects_nonconforming_output(self):
        client = StructuredProbeClient('{"ready":false}')
        result = ResponsesProviderAdapter(client).probe_capabilities()
        self.assertEqual(result, {"basic_response": False, "json_schema": False})

    def test_task_service_exposes_generation_preflight_without_workflow_authority(self):
        with tempfile.TemporaryDirectory() as root:
            pipeline = StubPipeline()
            service = TaskService(WorkspaceStore(root), generation_pipeline=pipeline)
            self.assertEqual(service.generation_core_health()["status"], "not_checked")
            result = service.initialize_generation_core()
            self.assertTrue(result["ready"])
            self.assertEqual(result["status"], "ready")
            self.assertEqual(pipeline.calls, 1)

    def test_unready_generation_core_closes_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            service = TaskService(WorkspaceStore(root), generation_pipeline=StubPipeline(False))
            result = service.initialize_generation_core()
            self.assertFalse(result["ready"])
            self.assertEqual(result["status"], "unavailable")

    def test_existing_task_service_flow_uses_generation_core_checkpoints(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            pipeline = GenerationPipeline(
                ModelGateway(ContractProvider(), model="contract-provider"),
                FileCheckpointStore(root / "checkpoints"),
                DeterministicRenderer(),
                TechnicalValidator(),
                asset_root=root,
            )
            service = TaskService(WorkspaceStore(root / "tasks"), generation_pipeline=pipeline)
            service.create("task", "manual")
            service.import_input("task", {"goal": "批准投入", "audience": "管理团队", "topic": "增长计划"})

            narrative = service.generate_narrative("task")["narrative"]
            self.assertEqual(narrative["metadata"]["generation_core"]["contract_name"], "narrative_spec_v1")
            service.confirm_narrative("task")
            outline = service.generate_outline("task")["outline"]
            self.assertEqual(outline["metadata"]["generation_core"]["contract_name"], "outline_spec_v1")
            service.confirm_outline("task")
            sample = service.generate_sample("task")["sample"]
            self.assertTrue(sample["metadata"]["post_render_gate"]["passed"])
            self.assertEqual(sample["metadata"]["generation_core"]["contract_name"], "sample_spec_v1")
            confirmation = service.confirm_sample("task")["confirmation"]
            self.assertEqual(confirmation["generation_core_confirmation"]["contract_name"], "frozen_sample_v1")
            deck = service.generate_deck("task")["deck"]
            self.assertTrue(deck["metadata"]["post_render_gate"]["passed"])
            self.assertEqual(deck["metadata"]["generation_core"]["contract_name"], "deck_spec_v1")
            self.assertTrue(all(deck["metadata"]["sample_pages_preserved"].values()))


if __name__ == "__main__":
    unittest.main()
