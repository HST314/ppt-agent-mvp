from e2e import test_ac_12_modes
from pathlib import Path


class AC13RoundLimitE2E(test_ac_12_modes.AC12ModesE2E):
    def test_auto_limit_waits_for_human_without_false_completion(self):
        inspector=test_ac_12_modes.SequenceInspector(); self.prepare(inspector)
        self.ok("/v1/tasks/journey/inspection/mode", {"mode":"auto"})
        result=self.ok("/v1/tasks/journey/inspection/run", {"max_rounds":2})
        self.assertEqual(result["rounds"],2); self.assertEqual(inspector.calls,3)
        self.assertFalse(result["report"]["passed"]); self.assertFalse(result["delivery_allowed"])
        self.assertEqual(result["state"]["status"],"waiting_for_user")
        self.assertEqual(result["state"]["waiting_reason"],"inspection_round_limit")
        status,page=self.call("GET","/tasks/journey/inspection")
        self.assertTrue(status.startswith("200")); self.assertIn('type="module"',page.decode())
        module=Path("frontend/static/js/stages/review.js").read_text()
        for token in ("修复轮次","暂不可交付","Agent 修复","整稿人工浏览","定位"): self.assertIn(token,module)
