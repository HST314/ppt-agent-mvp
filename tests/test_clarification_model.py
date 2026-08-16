import tempfile
import unittest

from ppt_agent.config import ClarificationConfig
from ppt_agent.errors import GatewayError, ValidationError
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class Clarifier:
    model = "clarifier-test"
    def __init__(self, questions=None, error=None): self.questions=questions or []; self.error=error; self.calls=[]
    def clarify(self, payload):
        self.calls.append(payload)
        if self.error: raise self.error
        return {"questions":self.questions,"model":self.model}


class SequentialClarifier:
    model = "sequential-clarifier"
    def __init__(self, rounds): self.rounds=list(rounds); self.calls=[]
    def clarify(self, payload):
        self.calls.append(payload)
        return {"questions":self.rounds.pop(0) if self.rounds else [],"model":self.model}


def question(field="decision", question_id="business-decision"):
    return {"question_id":question_id,"field_path":field,"prompt":"本次发布需要管理层批准预算还是仅同步进展？","helper_text":"这会决定论证结构和数据深度。","options":[{"value":"approve","label":"批准预算","description":"以决策材料为主"}],"allow_other":True,"blocking":True}


class ClarificationModelTests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory()
    def tearDown(self): self.tmp.cleanup()
    def service(self, clarifier):
        service=TaskService(WorkspaceStore(self.tmp.name),clarifier=clarifier); service.create("task"); return service
    def test_original_input_is_frozen_and_model_result_is_audited(self):
        gateway=Clarifier([question()]); service=self.service(gateway)
        imported=service.import_input("task","为新品发布制作管理层演示")
        self.assertEqual(imported["clarification"]["status"],"generating"); self.assertEqual(imported["clarification"]["details"],[])
        result=service.generate_clarification("task")
        self.assertIn("为新品发布",gateway.calls[0]["original_input"]); self.assertEqual(gateway.calls[0]["original_input_sha256"],result["input_hash"])
        self.assertEqual(result["question_source"],"model"); self.assertEqual(result["question_model"],"clarifier-test")
    def test_agent_mode_fails_closed_without_fallback(self):
        service=self.service(Clarifier(error=RuntimeError("timeout"))); service.import_input("task",{"topic":"新品"})
        with self.assertRaises(RuntimeError): service.generate_clarification("task")
        view=service.input_view("task"); self.assertEqual(view["clarification"]["status"],"failed"); self.assertIsNone(view["clarification"]["question_source"]); self.assertEqual(view["clarification"]["details"],[])
    def test_rejects_known_field_and_duplicate_field(self):
        service=self.service(Clarifier([question("topic")])); service.import_input("task",{"topic":"新品"})
        with self.assertRaises(ValidationError): service.generate_clarification("task")
        self.assertEqual(service.input_view("task")["clarification"]["status"],"failed")
    def test_fallback_requires_separate_service_action(self):
        service=self.service(Clarifier(error=RuntimeError("timeout"))); service.import_input("task",{"topic":"新品"})
        with self.assertRaises(RuntimeError): service.generate_clarification("task")
        result=service.use_fallback_clarification("task"); self.assertEqual(result["question_source"],"fallback"); self.assertTrue(result["details"])
    def test_gateway_error_semantics_and_correlation_survive_in_clarification(self):
        error=GatewayError(
            "模型服务请求过于频繁，请等待后重新探测",
            code="model_rate_limited",
            retryable=True,
            retry_after_seconds=23,
        )
        error.agent_audit_id="agent-audit-safe"
        service=self.service(Clarifier(error=error)); service.import_input("task",{"topic":"新品"})
        with self.assertRaises(GatewayError) as caught:
            service.generate_clarification("task")
        self.assertIs(caught.exception,error)
        persisted=service.input_view("task")["clarification"]["error"]
        self.assertEqual(persisted["code"],"model_rate_limited")
        self.assertEqual(persisted["diagnostic_id"],error.diagnostic_id)
        self.assertEqual(persisted["agent_audit_id"],"agent-audit-safe")
        self.assertTrue(persisted["retryable"])
        self.assertEqual(persisted["retry_after_seconds"],23)

    def multi_round_service(self, gateway, **overrides):
        config=ClarificationConfig(**{"max_questions_per_round":3,"max_rounds":3,"style":"comprehensive",**overrides})
        service=TaskService(WorkspaceStore(self.tmp.name),clarifier=gateway,clarification_config=config); service.create("task"); return service

    def test_multi_round_advances_until_model_returns_no_questions(self):
        gateway=SequentialClarifier([[question("audience","q-audience")],[question("pace","q-pace")],[]])
        service=self.multi_round_service(gateway)
        service.import_input("task","新品发布")
        first=service.generate_clarification("task")
        self.assertEqual(first["round"],1); self.assertFalse(first["confirmed"])
        context=gateway.calls[0]["clarification_context"]
        self.assertEqual((context["round"],context["max_rounds"],context["max_questions_per_round"],context["style"]),(1,3,3,"comprehensive"))
        self.assertEqual(context["previous_qa"],[]); self.assertIn("第 1/3 轮",context["directive"])

        answered=service.answer_clarifications("task",{"q-audience":{"option":"Other","other":"管理层"}})
        self.assertEqual(answered["status"],"generating"); self.assertEqual(answered["round"],2); self.assertFalse(answered["confirmed"])
        self.assertEqual(service.get("task")["required_action"],"wait_for_clarification")
        self.assertEqual(len(answered["rounds_history"]),1)

        second=service.generate_clarification("task")
        self.assertEqual(second["round"],2)
        context=gateway.calls[1]["clarification_context"]
        self.assertEqual(context["round"],2)
        self.assertEqual(len(context["previous_qa"]),1)
        self.assertEqual(context["previous_qa"][0]["answers"],{"q-audience":"管理层"})

        service.answer_clarifications("task",{"q-pace":{"option":"Other","other":"10 页以内"}})
        third=service.generate_clarification("task")
        self.assertTrue(third["confirmed"]); self.assertEqual(third["round"],3)
        view=service.input_view("task")
        self.assertTrue(view["clarification"]["confirmed"])
        self.assertEqual(view["task_card"]["audience"],"管理层")
        self.assertEqual(view["task_card"]["pace"],"10 页以内")
        self.assertEqual(view["state"]["status"],"ready")

    def test_multi_round_stops_at_max_rounds(self):
        gateway=SequentialClarifier([[question("audience","q-audience")],[question("pace","q-pace")]])
        service=self.multi_round_service(gateway,max_rounds=2,style="minimal")
        service.import_input("task","新品发布")
        first=service.generate_clarification("task")
        self.assertIn("仅提出真正阻碍交付",gateway.calls[0]["clarification_context"]["directive"])
        service.answer_clarifications("task",{"q-audience":{"option":"Other","other":"管理层"}})
        second=service.generate_clarification("task")
        done=service.answer_clarifications("task",{"q-pace":{"option":"Other","other":"10 页"}})
        self.assertTrue(done["confirmed"]); self.assertEqual(done["status"],"ready")
        self.assertEqual(service.get("task")["status"],"ready")
        self.assertEqual(len(gateway.calls),2)

    def test_fallback_questions_do_not_trigger_further_rounds(self):
        gateway=Clarifier(error=RuntimeError("down"))
        service=self.multi_round_service(gateway)
        service.import_input("task",{"topic":"新品"})
        with self.assertRaises(RuntimeError): service.generate_clarification("task")
        fallback=service.use_fallback_clarification("task")
        self.assertEqual(fallback["question_source"],"fallback")
        submitted={q["question_id"]:{"option":q["options"][0]["value"]} for q in fallback["details"]}
        done=service.answer_clarifications("task",submitted)
        self.assertTrue(done["confirmed"]); self.assertEqual(done["status"],"ready")
        self.assertEqual(len(gateway.calls),1)

    def test_question_limit_follows_config(self):
        gateway=SequentialClarifier([[question("audience","q-audience"),question("pace","q-pace")]])
        service=self.multi_round_service(gateway,max_questions_per_round=1)
        service.import_input("task","新品发布")
        with self.assertRaises(ValidationError): service.generate_clarification("task")
        self.assertEqual(service.input_view("task")["clarification"]["status"],"failed")

    def test_cross_round_field_repeat_is_rejected(self):
        gateway=SequentialClarifier([[question("pace","q-pace")],[question("pace","q-pace-2")]])
        service=self.multi_round_service(gateway)
        service.import_input("task","新品发布")
        service.generate_clarification("task")
        service.answer_clarifications("task",{"q-pace":{"option":"Other","other":"10 页"}})
        with self.assertRaises(ValidationError): service.generate_clarification("task")
        self.assertEqual(service.input_view("task")["clarification"]["status"],"failed")


if __name__ == "__main__": unittest.main()
