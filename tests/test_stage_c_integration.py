import json
import tempfile
import unittest
from pathlib import Path

from ppt_agent.errors import GatewayError, GatewayUnknownResult, ValidationError
from ppt_agent.audit import bind_agent_audit_context
from ppt_agent.gateways import AgentGateway, LockedSkillMetadataLoader
from ppt_agent.model_clients import ModelToolCall, ModelTurn
from ppt_agent.service import TaskService
from ppt_agent.skill_runtime import SkillRuntime
from ppt_agent.store import WorkspaceStore


class ScriptedClient:
    def __init__(self, *texts): self.turns = [ModelTurn(text, f"r-{index}") for index, text in enumerate(texts)]; self.inputs=[]
    def create(self, **kwargs):
        if kwargs.get("tool_choice") == {"type":"function", "name":"read_skill_file"}:
            return ModelTurn(None, "skill-entry", (ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "skill-entry"),))
        self.inputs.append(kwargs)
        return self.turns.pop(0)


class RaisingClient:
    def __init__(self, error): self.error = error
    def create(self, **kwargs): raise self.error


def service(root, client):
    skill = SkillRuntime.builtin(); gateway = AgentGateway(client, skill=skill, model="stage-c-test")
    return TaskService(WorkspaceStore(root), generator=gateway, builder=gateway, inspector=gateway, skills=LockedSkillMetadataLoader(skill))


def outline_response(count):
    return json.dumps({"slides":[{
        "title":f"第 {index + 1} 页","purpose":f"推进节点 {index + 1}",
        "content_markdown":f"- 内容 {index + 1}","resource_uris":[],
    } for index in range(count)]},ensure_ascii=False)


def narrative_response(*, goal="演示", audience="客户", topic="方案", extra=""):
    markdown = (
        f"# 叙事结构\n\n## 核心结论\n{topic}服务于{goal}，以已确认事实形成清晰、可验证的决策依据。"
        f"{extra}\n\n## 页面逻辑\n面向{audience}先建立背景与核心判断，再展开方案价值、证据与行动建议，"
        "让每一章节推进同一结论。"
    )
    return json.dumps({"markdown": markdown}, ensure_ascii=False)


class StageCIntegrationTests(unittest.TestCase):
    def test_structured_outline_is_validated_and_rendered_as_canonical_markdown(self):
        with tempfile.TemporaryDirectory() as root:
            client=ScriptedClient(narrative_response(),outline_response(3))
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{"goal":"演示","audience":"客户","topic":"方案","页数":3})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            outline=svc.generate_outline("task")["outline"]
            self.assertEqual(outline["slide_ids"],["slide-1","slide-2","slide-3"])
            self.assertIn("## [slide-1] 第 1 页",outline["markdown"])
            self.assertIn("- 页面目的：推进节点 1",outline["markdown"])
            self.assertNotIn('"slides"',outline["markdown"])

    def test_semantic_outline_failure_gets_one_correction_and_private_diagnostic(self):
        with tempfile.TemporaryDirectory() as root:
            client=ScriptedClient(narrative_response(),outline_response(1),outline_response(3))
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{"goal":"演示","audience":"客户","topic":"方案","页数":3})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            outline=svc.generate_outline("task")["outline"]
            self.assertEqual(len(outline["slide_ids"]),3)
            correction=json.loads(client.inputs[2]["input"][1]["content"])["semantic_correction"]
            self.assertIn("必须严格包含 3 页",correction["error"])
            diagnostics=svc.versions("task","outline-diagnostic")
            self.assertEqual(len(diagnostics),1)
            candidate=json.loads(svc.version("task",diagnostics[0]["hash"]))
            self.assertEqual(len(candidate["candidate"]["slides"]),1)
            self.assertFalse(diagnostics[0]["metadata"]["public_error_exposes_candidate"])

    def test_narrative_retries_disclosed_invented_stage_numbers_with_strict_allowlist(self):
        with tempfile.TemporaryDirectory() as root:
            client=ScriptedClient(
                narrative_response(goal="扩容汇报",audience="管理层",topic="AI 客服试点",extra="建议第 4 周完成启动，第 8 周扩展，阶段资源占比 45%（待确认）。"),
                narrative_response(goal="扩容汇报",audience="管理层",topic="AI 客服试点",extra="项目按启动阶段、扩展阶段与稳态阶段推进，总周期 12 周。"),
            )
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{"goal":"扩容汇报","audience":"管理层","topic":"AI 客服试点","项目周期":"12 周"})

            narrative=svc.generate_narrative("task")["narrative"]

            self.assertIn("总周期 12 周",narrative["markdown"])
            self.assertNotIn("45%",narrative["markdown"])
            self.assertEqual(len(client.inputs),2)
            initial=json.loads(client.inputs[0]["input"][1]["content"])
            allowed={item["value"].replace(" ","") for item in initial["narrative_numeric_policy"]["allowed_claims"]}
            self.assertIn("12周",allowed)
            correction=json.loads(client.inputs[1]["input"][1]["content"])["semantic_correction"]
            self.assertEqual({value.replace(" ","") for value in correction["forbidden_values"]},{"4周","8周","45%"})
            self.assertIn("不得改标为假设",correction["rule"])

    def test_narrative_invented_stage_numbers_remain_fail_closed_after_retry(self):
        with tempfile.TemporaryDirectory() as root:
            bad=narrative_response(goal="扩容汇报",audience="管理层",topic="AI 客服试点",extra="建议第 3 周完成启动，第 6 周扩展（均待确认）。")
            client=ScriptedClient(bad,bad)
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{"goal":"扩容汇报","audience":"管理层","topic":"AI 客服试点","项目周期":"12 周"})

            with self.assertRaisesRegex(ValidationError,"未绑定事实"):
                svc.generate_narrative("task")

            self.assertEqual(len(client.inputs),2)
            self.assertFalse(svc.versions("task","narrative"))

    def test_semantic_outline_correction_is_bounded_to_one_retry(self):
        with tempfile.TemporaryDirectory() as root:
            client=ScriptedClient(narrative_response(),outline_response(1),outline_response(1))
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{"goal":"演示","audience":"客户","topic":"方案","页数":3})
            svc.generate_narrative("task"); svc.confirm_narrative("task")
            with self.assertRaises(ValidationError): svc.generate_outline("task")
            self.assertEqual(len(client.inputs),3)
            self.assertEqual(len(svc.versions("task","outline-diagnostic")),2)
            self.assertFalse(svc.versions("task","outline"))

    def test_weak_narrative_gets_one_bounded_semantic_structure_correction(self):
        with tempfile.TemporaryDirectory() as root:
            client=ScriptedClient(
                '{"markdown":"需要分析规划摘要来决定叙事结构。"}',
                narrative_response(goal="扩容决策",audience="管理层",topic="AI 客服试点"),
            )
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{"goal":"扩容决策","audience":"管理层","topic":"AI 客服试点"})

            narrative=svc.generate_narrative("task")["narrative"]

            self.assertTrue(narrative["metadata"]["narrative_quality"]["passed"])
            self.assertEqual(len(client.inputs),2)
            correction=json.loads(client.inputs[1]["input"][1]["content"])["semantic_correction"]
            self.assertIn("最低语义/结构门禁",correction["error"])
            self.assertEqual(set(correction["missing_context_fields"]),{"topic","goal","audience"})

    def test_persistently_weak_narrative_fails_closed_without_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            weak='{"markdown":"需要分析规划摘要来决定叙事结构。"}'
            client=ScriptedClient(weak,weak)
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{"goal":"扩容决策","audience":"管理层","topic":"AI 客服试点"})

            with self.assertRaisesRegex(ValidationError,"最低语义/结构门禁"):
                svc.generate_narrative("task")

            self.assertEqual(len(client.inputs),2)
            self.assertFalse(svc.versions("task","narrative"))

    def test_narrative_correction_copies_frozen_context_verbatim(self):
        with tempfile.TemporaryDirectory() as root:
            client=ScriptedClient(
                narrative_response(goal="扩容决策",audience="管理层",topic="AI客服试点"),
                narrative_response(goal="扩容决策",audience="管理层",topic="AI 客服试点"),
            )
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{"goal":"扩容决策","audience":"管理层","topic":"AI 客服试点"})

            narrative=svc.generate_narrative("task")["narrative"]

            self.assertIn("AI 客服试点",narrative["markdown"])
            self.assertEqual(len(client.inputs),2)
            correction=json.loads(client.inputs[1]["input"][1]["content"])["semantic_correction"]
            self.assertEqual(correction["missing_context_fields"],["topic"])
            self.assertEqual(correction["required_context_verbatim"],[
                {"field":"topic","value":"AI 客服试点"},
                {"field":"goal","value":"扩容决策"},
                {"field":"audience","value":"管理层"},
            ])
            self.assertIn("逐字写入正文",correction["rule"])

    def test_long_topic_is_materialized_verbatim_after_bounded_correction(self):
        with tempfile.TemporaryDirectory() as root:
            topic="AI 客服助手试点成效、扩容计划、预算、风险与管理层决策"
            compact=narrative_response(goal="扩容决策",audience="管理层",topic="AI 客服助手扩容决策")
            client=ScriptedClient(compact,compact)
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{"goal":"扩容决策","audience":"管理层","topic":topic})

            narrative=svc.generate_narrative("task")["narrative"]

            self.assertEqual(len(client.inputs),2)
            self.assertIn(topic,narrative["markdown"])
            self.assertEqual(
                {item["field"] for item in narrative["metadata"]["narrative_quality"]["required_context"]},
                {"topic","goal","audience"},
            )
            correction=json.loads(client.inputs[1]["input"][1]["content"])["semantic_correction"]
            self.assertIn(topic,correction["required_context_markdown_block"])
            self.assertIn("冻结受众",correction["required_context_markdown_block"])

    def test_real_card_audience_alias_reaches_three_field_narrative_contract(self):
        with tempfile.TemporaryDirectory() as root:
            topic="AI 客服助手试点成效、扩容计划、预算、风险与管理层决策"
            client=ScriptedClient(narrative_response(
                goal="请管理层批准 AI 客服助手第二阶段扩容方案与预算",
                audience="CEO、CFO 与客户运营负责人",
                topic=topic,
            ))
            svc=service(root,client); svc.create("task","manual")
            imported=svc.import_input("task",{
                "演示目标":"请管理层批准 AI 客服助手第二阶段扩容方案与预算",
                "主要受众":"CEO、CFO 与客户运营负责人",
                "核心主题":topic,
            })

            narrative=svc.generate_narrative("task")["narrative"]

            self.assertEqual(imported["task_card"]["missing"],[])
            self.assertEqual(
                {item["field"] for item in narrative["metadata"]["narrative_quality"]["required_context"]},
                {"topic","goal","audience"},
            )

    def test_outline_correction_returns_all_budget_claims_verbatim(self):
        with tempfile.TemporaryDirectory() as root:
            first=outline_response(6)
            second_value=json.loads(outline_response(6))
            second_value["slides"][1]["content_markdown"]="总预算 80 万元：软件 36 万元、服务 24 万元、实施 12 万元、培训 8 万元。"
            client=ScriptedClient(
                narrative_response(goal="批准扩容",audience="管理层",topic="扩容决策"),
                first,
                json.dumps(second_value,ensure_ascii=False),
            )
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{
                "goal":"批准扩容","audience":"管理层","topic":"扩容决策","页数":6,
                "总预算":"80 万元","软件费用":"36 万元","服务费用":"24 万元",
                "实施费用":"12 万元","培训费用":"8 万元",
            })
            svc.generate_narrative("task"); svc.confirm_narrative("task")

            outline=svc.generate_outline("task")["outline"]

            self.assertIn("24 万元",outline["markdown"])
            correction=json.loads(client.inputs[2]["input"][1]["content"])["semantic_correction"]
            all_values={item["value"] for item in correction["required_claims_verbatim"]}
            missing_values={item["value"] for item in correction["missing_required_claims_verbatim"]}
            self.assertTrue({"80 万元","36 万元","24 万元","12 万元","8 万元"}.issubset(all_values))
            self.assertTrue({"80 万元","36 万元","24 万元","12 万元","8 万元"}.issubset(missing_values))

    def test_outline_persistent_budget_omission_remains_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            missing=outline_response(6)
            client=ScriptedClient(
                narrative_response(goal="批准扩容",audience="管理层",topic="扩容决策"),
                missing,
                missing,
            )
            svc=service(root,client); svc.create("task","manual")
            svc.import_input("task",{
                "goal":"批准扩容","audience":"管理层","topic":"扩容决策","页数":6,
                "总预算":"80 万元","软件费用":"36 万元","服务费用":"24 万元",
                "实施费用":"12 万元","培训费用":"8 万元",
            })
            svc.generate_narrative("task"); svc.confirm_narrative("task")

            with self.assertRaisesRegex(ValidationError,"遗漏必需事实"):
                svc.generate_outline("task")

            self.assertEqual(len(svc.versions("task","outline-diagnostic")),2)
            self.assertFalse(svc.versions("task","outline"))
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
            svc = service(root, ScriptedClient(narrative_response()))
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
            svc=service(root,ScriptedClient(narrative_response(goal="g",audience="a",topic="t")))
            svc.create("task"); svc.import_input("task",{"goal":"g","audience":"a","topic":"t"})
            svc.generate_narrative("task")
            first=WorkspaceStore(root).agent_audits()
            self.assertEqual(first[-1]["events"][-1]["reason"],"success")
            self.assertTrue((Path(root)/"task"/"agent-audit.jsonl").is_file())
            self.assertEqual(WorkspaceStore(root).agent_audits(task_id="task"),first)
            failing=service(root,RaisingClient(GatewayUnknownResult("unknown")))
            with self.assertRaises(GatewayUnknownResult): failing.generate_narrative("task")
            persisted=WorkspaceStore(root).agent_audits()
            self.assertEqual(len(persisted),len(first)+1)
            self.assertEqual(persisted[-1]["events"][-1]["reason"],"gateway_unknown_result")
            self.assertIn("input_sha256",persisted[-1]["events"][0])

    def test_agent_audit_is_correlated_to_task_and_job_and_error(self):
        with tempfile.TemporaryDirectory() as root:
            store=WorkspaceStore(root)
            failure=GatewayError(
                "failed",
                code="model_authentication_failed",
                audit_details={
                    "category":"authentication",
                    "http_status":401,
                    "sdk_exception_type":"APIStatusError",
                    "provider_request_id_sha256":"a"*64,
                    "retryable":False,
                    "raw_provider_error":"must-not-persist",
                },
            )
            gateway=AgentGateway(RaisingClient(failure),skill=SkillRuntime.builtin())
            gateway.set_audit_sink(store.append_agent_audit)
            with bind_agent_audit_context(task_id="task",job_id="job_123"):
                with self.assertRaises(GatewayError) as caught:
                    gateway.generate("narrative",{"task_id":"task"},skill="")
            audit=store.agent_audits(task_id="task",job_id="job_123")
            self.assertEqual(len(audit),1)
            self.assertEqual(audit[0]["audit_id"],caught.exception.agent_audit_id)
            self.assertEqual(caught.exception.public()["error"]["agent_audit_id"],audit[0]["audit_id"])
            self.assertEqual(audit[0]["error_code"],"model_authentication_failed")
            self.assertEqual(audit[0]["events"][-1]["http_status"],401)
            self.assertEqual(audit[0]["events"][-1]["category"],"authentication")
            self.assertNotIn("content",json.dumps(audit))
            self.assertNotIn("must-not-persist",json.dumps(audit))


if __name__ == "__main__": unittest.main()
