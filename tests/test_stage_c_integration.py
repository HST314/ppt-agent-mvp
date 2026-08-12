import json
import tempfile
import unittest

from ppt_agent.errors import GatewayError, GatewayUnknownResult
from ppt_agent.gateways import AgentGateway, LockedSkillMetadataLoader
from ppt_agent.model_clients import ModelTurn
from ppt_agent.service import TaskService
from ppt_agent.skill_runtime import SkillRuntime
from ppt_agent.store import WorkspaceStore


class ScriptedClient:
    def __init__(self, *texts): self.turns = [ModelTurn(text, f"r-{index}") for index, text in enumerate(texts)]
    def create(self, **kwargs): return self.turns.pop(0)


class RaisingClient:
    def __init__(self, error): self.error = error
    def create(self, **kwargs): raise self.error


def service(root, client):
    skill = SkillRuntime.builtin(); gateway = AgentGateway(client, skill=skill, model="stage-c-test")
    return TaskService(WorkspaceStore(root), generator=gateway, builder=gateway, inspector=gateway, skills=LockedSkillMetadataLoader(skill))


class StageCIntegrationTests(unittest.TestCase):
    def test_real_mode_narrative_commits_only_current_stage_then_waits_at_manual_gate(self):
        with tempfile.TemporaryDirectory() as root:
            svc = service(root, ScriptedClient('{"markdown":"# 叙事结构\\n"}'))
            svc.create("task", "manual")
            svc.import_input("task", {"goal":"演示", "audience":"客户", "topic":"方案", "页数":3})
            result = svc.generate_narrative("task")
            self.assertEqual(result["state"]["stage"], "narrative")
            self.assertEqual(result["state"]["status"], "waiting_for_user")
            self.assertEqual(result["state"]["required_action"], "approve_narrative")
            self.assertEqual(len(svc.versions("task", "narrative")), 1)
            self.assertFalse(svc.versions("task", "outline"))

    def test_cross_stage_claim_is_rejected_without_artifact_or_state_advance(self):
        with tempfile.TemporaryDirectory() as root:
            svc = service(root, ScriptedClient('{"markdown":"# x","confirmed":true}'))
            svc.create("task", "manual")
            svc.import_input("task", {"goal":"演示", "audience":"客户", "topic":"方案"})
            before = svc.get("task")
            with self.assertRaises(GatewayError): svc.generate_narrative("task")
            self.assertEqual(svc.get("task"), before)
            self.assertFalse(svc.versions("task", "narrative"))

    def test_unknown_gateway_result_does_not_advance_or_partially_commit(self):
        with tempfile.TemporaryDirectory() as root:
            svc = service(root, RaisingClient(GatewayUnknownResult("unknown")))
            svc.create("task", "manual")
            svc.import_input("task", {"goal":"演示", "audience":"客户", "topic":"方案"})
            before = svc.get("task")
            with self.assertRaises(GatewayUnknownResult): svc.generate_narrative("task")
            self.assertEqual(svc.get("task"), before)
            self.assertFalse(svc.versions("task", "narrative"))

    def test_inspection_gateway_cannot_return_html_or_workflow_fields(self):
        gateway = AgentGateway(ScriptedClient('{"passed":true,"issues":[],"html":"bad"}'), skill=SkillRuntime.builtin())
        with self.assertRaises(GatewayError): gateway.inspect("outline", "<html></html>")


if __name__ == "__main__": unittest.main()
