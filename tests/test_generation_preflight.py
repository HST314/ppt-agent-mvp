import unittest

from ppt_agent.generation_preflight import (
    hard_browser_blockers,
    inspect_layout_capacity,
    layout_capacity_policy,
    structured_canonical_blockers,
)


class GenerationPreflightTests(unittest.TestCase):
    def test_framework_does_not_own_design_capacity_or_canonical_style_rules(self):
        contract = {"slide_ids": ["slide-1"]}
        html = '<section class="slide" data-slide-id="slide-1"><h1>自由设计</h1></section>'
        self.assertEqual(layout_capacity_policy(contract), {})
        self.assertEqual(structured_canonical_blockers({"errors": ["style"]}, contract), [])
        evidence = inspect_layout_capacity(html, contract)
        self.assertTrue(evidence["passed"])
        self.assertFalse(evidence["applicable"])

    def test_objective_browser_failures_remain_hard_blockers(self):
        blockers = hard_browser_blockers({
            "available": True,
            "passed": False,
            "issues": [{"code": "content_out_of_bounds", "severity": "blocker"}],
        })
        self.assertEqual([item["code"] for item in blockers], ["content_out_of_bounds"])


if __name__ == "__main__":
    unittest.main()
