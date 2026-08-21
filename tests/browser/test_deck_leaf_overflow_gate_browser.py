"""真实合稿 leaf 溢出的端到端门禁。

复刻真实复验场景：两批 builder 成功返回后，合稿在 Chromium 硬门禁被
`element_scroll_overflow`（典型 52px -> 61px 小标签）阻断、全稿无法落库。
确定性 autofit 必须定点修复 leaf 溢出并让候选全稿真实落库，
review / finalize / delivery 全程可达。
"""

import tempfile
import unittest

from ppt_agent.browser_inspection import ChromiumDeckInspector
from ppt_agent.render_gate import canonical_post_render_evidence
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class PassingInspector:
    requires_browser_evidence = True

    def inspect(self, _outline, _html, *, browser_evidence=None):
        return {"passed": True, "issues": [], "model": "passing", "browser_received": browser_evidence is not None}


# 模板 h1（52px 衬线标题）在缺少 CJK 字体的环境中会被误判滚动溢出，
# 夹具与既有 autofit 门禁一致使用带充足余量的 div 标题。
_TITLE = '<div data-element-id="title" style="font-size:32px;line-height:1.2;height:56px;overflow:hidden">%s</div>'
_LEAF = (
    '<div data-element-id="kpi-label" style="position:absolute;left:77px;top:200px;width:400px;height:52px;overflow:hidden;font-size:16px;line-height:1.4">'
    '<div style="height:61px">一个会在 52px 盒子里溢出到 61px 的小标签内容</div></div>'
)
_LEAF_SELECTOR = '.slide[data-slide-id="slide-4"] [data-element-id="kpi-label"]'


def _slide(slide_id, inner):
    return f'<section class="slide light" id="{slide_id}" data-slide-id="{slide_id}">{inner}</section>'


class LeafOverflowBuilder:
    """模拟真实分批 builder：其中一张未确认页携带典型 52px -> 61px leaf 溢出。"""

    def __init__(self, overflow_slide):
        self.overflow_slide = overflow_slide
        self.calls = []

    def build(self, outline, **context):
        self.calls.append(dict(context))
        sections = []
        for slide_id in context["slide_ids"]:
            inner = _TITLE % f"{slide_id} 标题" + '<p data-element-id="body">要点内容</p>'
            if context.get("action") == "deck" and slide_id == self.overflow_slide:
                inner += _LEAF
            sections.append(_slide(slide_id, inner))
        return "<!doctype html><html><head><meta charset='utf-8'></head><body>" + "".join(sections) + "</body></html>"


class DeckLeafOverflowGate(unittest.TestCase):
    def test_merged_leaf_overflow_is_autofit_and_deck_reaches_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            svc = TaskService(
                WorkspaceStore(root),
                inspector=PassingInspector(),
                browser_inspector=ChromiumDeckInspector(),
            )
            svc.create("task", "manual")
            svc.import_input("task", {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 8})
            svc.generate_narrative("task")
            svc.confirm_narrative("task")
            svc.generate_outline("task")
            svc.confirm_outline("task")
            svc.select_samples("task", ["slide-2", "slide-7"])
            svc.generate_sample("task")
            svc.confirm_sample("task")
            builder = LeafOverflowBuilder("slide-4")
            svc.builder = builder

            deck = svc.generate_deck("task")["deck"]

            # 未确认页按 3 页分批（与真实 builder 路径一致），合稿真实落库。
            batches = [call["slide_ids"] for call in builder.calls if call.get("action") == "deck"]
            self.assertEqual(batches, [["slide-1", "slide-3", "slide-4"], ["slide-5", "slide-6", "slide-8"]])
            self.assertIn(deck["hash"], {item["hash"] for item in svc.versions("task", "deck")})

            gate = deck["metadata"]["post_render_gate"]
            self.assertTrue(gate["passed"])
            self.assertEqual(gate["blocker_count"], 0)
            self.assertEqual(gate["geometry"]["overflow_count"], 0)
            autofit = gate["overflow_autofit"]
            self.assertIsNotNone(autofit)
            leaf_rule = autofit["rules"].get(_LEAF_SELECTOR)
            self.assertIsNotNone(leaf_rule, autofit["rules"])
            self.assertIn("height: 62.00px !important", leaf_rule)
            self.assertNotIn("zoom", leaf_rule)
            self.assertTrue(autofit["converged"])
            self.assertEqual(autofit["remaining"], [])
            self.assertEqual(canonical_post_render_evidence(gate), svc.version("task", gate["evidence_hash"]))
            self.assertIn(gate["evidence_hash"], {item["hash"] for item in svc.versions("task", "post-render-gate-evidence")})

            # review / finalize / delivery 全程可达。
            inspection = svc.run_inspection("task", 0)["report"]
            self.assertTrue(inspection["passed"])
            finalization = svc.finalize_deck("task", deck["hash"], "review")["finalization"]
            self.assertEqual(finalization["deck_hash"], deck["hash"])
            delivery = svc.publish_delivery("task")["delivery"]
            self.assertIn("post-render-gate-evidence.json", delivery["files"])


if __name__ == "__main__":
    unittest.main()
