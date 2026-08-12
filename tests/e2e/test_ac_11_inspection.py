import json

from e2e.support import SampleJourney
from ppt_agent.service import TaskService


class Inspector:
    def __init__(self): self.calls=[]
    def inspect(self, original_outline, html):
        self.calls.append({"outline":original_outline,"html":html})
        return {"passed":False,"model":"independent-fixture","issues":[
            {"issue_id":"element-overflow","severity":"blocker","level":"element","code":"overflow","message":"元素溢出","slide_id":"slide-1","element_id":"title","evidence":"超出边界 12px","suggestion":"缩小字号"},
            {"issue_id":"slide-density","severity":"warning","level":"slide","code":"density","message":"页面过密","slide_id":"slide-2","evidence":"元素数超过阈值","suggestion":"拆分页面"},
            {"issue_id":"deck-consistency","severity":"warning","level":"deck","code":"consistency","message":"整稿不一致","slide_id":"","evidence":"标题字号存在差异","suggestion":"统一标题层级"}]}


class AC11InspectionE2E(SampleJourney):
    def test_isolated_three_level_report_and_hash_staleness(self):
        inspector=Inspector(); self.app.service=TaskService(self.store,inspector=inspector)
        self.ok("/v1/tasks/journey/samples/generate", {})
        self.ok("/v1/tasks/journey/samples/confirm", {})
        self.ok("/v1/tasks/journey/deck/generate", {})
        result=self.ok("/v1/tasks/journey/inspection/run", {"max_rounds":0})
        self.assertEqual(set(inspector.calls[0]),{"outline","html"})
        self.assertEqual({x["level"] for x in result["report"]["issues"]},{"element","slide","deck"})
        self.assertEqual(result["report"]["deck_hash"],result["deck"]["hash"])
        self.assertEqual(result["report"]["metadata"]["scope"],"full")
        self.ok("/v1/tasks/journey/deck/modify", {"prompt":"统一背景色","change_type":"visual","scope":"global","slide_ids":[],"element_id":None})
        stale=self.get_json("/v1/tasks/journey/inspection")
        self.assertTrue(stale["report"]["stale"]); self.assertFalse(stale["delivery_allowed"])

