import tempfile
import unittest

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


def question(field="decision"):
    return {"question_id":"business-decision","field_path":field,"prompt":"本次发布需要管理层批准预算还是仅同步进展？","helper_text":"这会决定论证结构和数据深度。","options":[{"value":"approve","label":"批准预算","description":"以决策材料为主"}],"allow_other":True,"blocking":True}


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


if __name__ == "__main__": unittest.main()
