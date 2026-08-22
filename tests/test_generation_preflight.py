import unittest

from ppt_agent.claim_ledger import build_claim_ledger
from ppt_agent.design_contract import build_presentation_technical_contract
from ppt_agent.p2 import canonical, digest
from ppt_agent.p4 import apply_presentation_technical_contract
from ppt_agent.render_gate import TechnicalGate


class AdvisoryBrowserInspector:
    def inspect(self, html, expected_slide_ids):
        return {
            "available": True,
            "passed": False,
            "engine": "chromium",
            "engine_version": "test",
            "viewport": {"width": 1280, "height": 720},
            "issues": [
                {
                    "code": "title_too_small",
                    "severity": "blocker",
                    "slide_id": expected_slide_ids[0],
                    "evidence": "font-size=24px",
                },
                {
                    "code": "dom_signature_mismatch",
                    "severity": "blocker",
                    "slide_id": expected_slide_ids[0],
                    "evidence": "Skill-specific shape differs",
                },
            ],
        }


class GenerationPreflightTests(unittest.TestCase):
    def test_objective_browser_failures_remain_hard_blockers(self):
        blockers = TechnicalGate.browser_blockers({
            "available": True,
            "passed": False,
            "issues": [{"code": "content_out_of_bounds", "severity": "blocker"}],
        })
        self.assertEqual([item["code"] for item in blockers], ["content_out_of_bounds"])

    def test_typography_severity_label_cannot_promote_an_advisory(self):
        blockers = TechnicalGate.browser_blockers({
            "available": True,
            "passed": False,
            "issues": [{"code": "text_too_small", "severity": "blocker"}],
        })
        self.assertEqual(blockers, [])

    def test_dom_shape_claims_and_typography_are_advisory_evidence(self):
        input_hash = "1" * 64
        outline_hash = "2" * 64
        contract = build_presentation_technical_contract(
            task_id="task",
            input_snapshot_hash=input_hash,
            outline_hash=outline_hash,
            slide_ids=["slide-1"],
            created_at="2026-08-22T00:00:00+00:00",
        )
        contract_hash = digest(canonical(contract))
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash=input_hash,
            source_binding="预算 100 万元",
            created_at="2026-08-22T00:00:00+00:00",
        )
        ledger_hash = digest(canonical(ledger))
        html = apply_presentation_technical_contract(
            '<!doctype html><html><head></head><body><section class="slide" id="slide-1"><p>另报 80 万元</p></section></body></html>',
            contract,
            contract_hash,
        )
        claim_id = ledger["claims"][0]["claim_id"]
        gate = TechnicalGate.evaluate(
            html,
            expected_slide_ids=["slide-1"],
            contract=contract,
            contract_hash=contract_hash,
            claim_ledger=ledger,
            claim_ledger_hash=ledger_hash,
            required_claim_ids=[claim_id],
            required_claim_ids_by_slide={"slide-1": [claim_id]},
            html_by_slide={"slide-1": '<section class="slide" id="slide-1"><p>另报 80 万元</p></section>'},
            browser_inspector=AdvisoryBrowserInspector(),
        )

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["blockers"], [])
        self.assertNotIn("canonical_validator", gate)
        self.assertTrue(gate["claims"]["advisory"])
        self.assertTrue(gate["geometry"]["passed"])
        self.assertEqual(
            {item["code"] for item in gate["advisories"]},
            {"unbound_claim", "missing_required_claim", "title_too_small", "dom_signature_mismatch"},
        )


if __name__ == "__main__":
    unittest.main()
