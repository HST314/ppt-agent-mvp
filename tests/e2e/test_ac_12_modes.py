from e2e.support import SampleJourney
from ppt_agent.service import TaskService
from ppt_agent.errors import GatewayUnknownResult


ISSUE={"issue_id":"overflow","severity":"blocker","level":"element","code":"overflow","message":"溢出","slide_id":"slide-1","element_id":"title","evidence":"超出边界","suggestion":"缩小字号"}

class SequenceInspector:
    def __init__(self, fail_count=99): self.calls=0; self.fail_count=fail_count
    def inspect(self, outline, html):
        self.calls+=1; failed=self.calls<=self.fail_count
        return {"passed":not failed,"issues":[ISSUE] if failed else [],"model":"sequence"}

class UnknownRepairBuilder:
    def build(self, outline, **context):
        raise GatewayUnknownResult("repair result unknown")

class AC12ModesE2E(SampleJourney):
    def prepare(self,inspector):
        self.app.service=TaskService(self.store,inspector=inspector)
        self.ok("/v1/tasks/journey/samples/generate", {}); self.ok("/v1/tasks/journey/samples/confirm", {}); self.ok("/v1/tasks/journey/deck/generate", {})

    def test_manual_suggests_without_fix_and_auto_is_bounded(self):
        inspector=SequenceInspector(1); self.prepare(inspector)
        manual=self.ok("/v1/tasks/journey/inspection/run", {"max_rounds":3})
        self.assertEqual(manual["rounds"],0); self.assertEqual(len(manual["reports"]),2)
        self.ok("/v1/tasks/journey/inspection/mode", {"mode":"auto"})
        auto=self.ok("/v1/tasks/journey/inspection/run", {"max_rounds":2})
        self.assertTrue(auto["report"]["passed"]); self.assertEqual(auto["rounds"],0)

    def test_disposition_audit_and_delivery_blocker_gate(self):
        self.prepare(SequenceInspector())
        result=self.ok("/v1/tasks/journey/inspection/run", {"max_rounds":0})
        status,raw=self.call("POST","/v1/tasks/journey/inspection/delivery-gate",{})
        self.assertTrue(status.startswith("409"),raw)
        disposed=self.ok("/v1/tasks/journey/issues/overflow/disposition", {"action":"waive","rationale":"用户接受该版式风险"})
        item=disposed["dispositions"][-1]
        self.assertEqual(item["actor"],"user"); self.assertEqual(item["target_deck_hash"],result["deck"]["hash"])
        self.assertTrue(self.ok("/v1/tasks/journey/inspection/delivery-gate", {})["delivery_allowed"])

    def test_agent_fix_unknown_does_not_publish_disposition_or_deck(self):
        self.prepare(SequenceInspector())
        self.ok("/v1/tasks/journey/inspection/run", {"max_rounds":0})
        self.app.service.builder=UnknownRepairBuilder()
        before_state=self.app.service.get("journey")
        before_decks=self.app.service.versions("journey","deck")
        before_dispositions=self.app.service.versions("journey","issue-disposition")
        before_events=self.app.service.events("journey")

        with self.assertRaises(GatewayUnknownResult):
            self.app.service.dispose_issue("journey","overflow","agent_fix","")

        self.assertEqual(self.app.service.get("journey"),before_state)
        self.assertEqual(self.app.service.versions("journey","deck"),before_decks)
        self.assertEqual(self.app.service.versions("journey","issue-disposition"),before_dispositions)
        self.assertEqual(self.app.service.events("journey"),before_events)
