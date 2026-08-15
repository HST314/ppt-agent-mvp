import json
import tempfile
import unittest

from ppt_agent.errors import GatewayError, GatewayUnknownResult
from ppt_agent.audit import bind_agent_audit_context
from ppt_agent.gateways import AgentGateway, LockedSkillMetadataLoader
from ppt_agent.model_clients import ModelTurn
from ppt_agent.service import TaskService
from ppt_agent.skill_runtime import SkillRuntime
from ppt_agent.store import WorkspaceStore


class ScriptedClient:
    def __init__(self, *texts): self.turns = [ModelTurn(text, f"r-{index}") for index, text in enumerate(texts)]; self.inputs=[]
    def create(self, **kwargs): self.inputs.append(kwargs); return self.turns.pop(0)


class RaisingClient:
    def __init__(self, error): self.error = error
    def create(self, **kwargs): raise self.error


def service(root, client):
    skill = SkillRuntime.builtin(); gateway = AgentGateway(client, skill=skill, model="stage-c-test")
    return TaskService(WorkspaceStore(root), generator=gateway, builder=gateway, inspector=gateway, skills=LockedSkillMetadataLoader(skill))


class StageCIntegrationTests(unittest.TestCase):
    def test_reported_natural_language_input_uses_tool_free_agent_contract(self):
        with tempfile.TemporaryDirectory() as root:
            client=ScriptedClient('{"questions":[]}')
            gateway=AgentGateway(client,skill=SkillRuntime.builtin(),model="provider-contract")
            svc=TaskService(WorkspaceStore(root),clarifier=gateway)
            svc.create("reported-input")
            imported=svc.import_input("reported-input","设计一个用于北工大集成电路学院介绍的ppt","markdown")
            result=svc.generate_clarification("reported-input")
            self.assertEqual(result["status"],"ready")
            self.assertEqual(client.inputs[0]["tools"],[])
            self.assertIn("北工大集成电路学院介绍",client.inputs[0]["input"][1]["content"])
            self.assertEqual(imported["task_card"]["missing"],["audience"])

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

    def test_agent_audit_survives_service_restart_and_records_failures(self):
        with tempfile.TemporaryDirectory() as root:
            svc=service(root,ScriptedClient('{"markdown":"# ok"}'))
            svc.create("task"); svc.import_input("task",{"goal":"g","audience":"a","topic":"t"})
            svc.generate_narrative("task")
            first=WorkspaceStore(root).agent_audits()
            self.assertEqual(first[-1]["events"][-1]["reason"],"success")
            failing=service(root,RaisingClient(GatewayUnknownResult("unknown")))
            with self.assertRaises(GatewayUnknownResult): failing.generate_narrative("task")
            persisted=WorkspaceStore(root).agent_audits()
            self.assertEqual(len(persisted),len(first)+1)
            self.assertEqual(persisted[-1]["events"][-1]["reason"],"gateway_unknown_result")
            self.assertIn("input_sha256",persisted[-1]["events"][0])

    def test_agent_audit_is_correlated_to_task_and_job_and_error(self):
        with tempfile.TemporaryDirectory() as root:
            store=WorkspaceStore(root)
            gateway=AgentGateway(RaisingClient(GatewayError("failed")),skill=SkillRuntime.builtin())
            gateway.set_audit_sink(store.append_agent_audit)
            with bind_agent_audit_context(task_id="task",job_id="job_123"):
                with self.assertRaises(GatewayError) as caught:
                    gateway.generate("narrative",{"task_id":"task"},skill="")
            audit=store.agent_audits(task_id="task",job_id="job_123")
            self.assertEqual(len(audit),1)
            self.assertEqual(audit[0]["audit_id"],caught.exception.agent_audit_id)
            self.assertEqual(caught.exception.public()["error"]["agent_audit_id"],audit[0]["audit_id"])
            self.assertNotIn("content",json.dumps(audit))


if __name__ == "__main__": unittest.main()
