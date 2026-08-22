import unittest

from ppt_agent.generation_preflight import inspect_layout_capacity, layout_capacity_policy


class GenerationPreflightTests(unittest.TestCase):
    def test_body_capacity_contract_blocks_overloaded_list_without_hiding_content(self):
        contract={
            "slide_contracts":[{
                "slide_id":"slide-1","layout_id":"A03","visual_role":"body",
            }],
        }
        items="".join(f"<li>第 {index} 项必须保持可见的业务说明</li>" for index in range(1,19))
        html=f'<!doctype html><html><body><section class="slide" data-slide-id="slide-1"><h1>高密度页面</h1><ul>{items}</ul></section></body></html>'

        evidence=inspect_layout_capacity(html,contract)

        self.assertFalse(evidence["passed"])
        issue=next(item for item in evidence["issues"] if item["metric"]=="list_items")
        self.assertEqual((issue["actual"],issue["maximum"]),(18,16))
        self.assertEqual(layout_capacity_policy(contract)["slide-1"]["minimum_body_font_px"],16)
        self.assertIn("不得隐藏",issue["suggestion"])


if __name__ == "__main__":
    unittest.main()
