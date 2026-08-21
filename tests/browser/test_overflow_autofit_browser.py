"""确定性溢出修复的 Chromium 门禁。

对应测试报告中的成品页溢出（第 2 页越界 24px、第 4 页滚动溢出 3px）：
修复器必须在真实浏览器测量下清除几何阻断，且复检全绿、重复执行幂等。
"""

import unittest

from ppt_agent.browser_inspection import ChromiumDeckInspector
from ppt_agent.overflow_autofit import fit_deck_html
from ppt_agent.p4 import assemble_locked_template, validate_html

SLIDE_IDS = ["slide-1", "slide-2", "slide-3"]


def _slide(slide_id, inner):
    return f'<section class="slide light" id="{slide_id}" data-slide-id="{slide_id}">{inner}</section>'


# 模板 h1（52px 衬线标题）在缺少 CJK 字体的环境中会因字体度量差异被误判
# 滚动溢出（client 58 vs scroll 66）。夹具改用带充足高度余量的 div 标题，
# 保证在任何字体环境下只保留有意的几何缺陷。
_TITLE = '<div data-element-id="title" style="font-size:32px;line-height:1.2;height:56px;overflow:hidden">%s</div>'


def overflowing_deck():
    clean = _slide("slide-1", _TITLE % "结论页" + '<p data-element-id="body">正常内容</p>')
    # 同时越界（底部超出 20px）且滚动溢出（内容 760px > 盒高 700px）的复合目标，
    # 且通过内联样式给定尺寸（真实 LLM 全稿的常见形态）。
    oob = _slide(
        "slide-2",
        _TITLE % "越界页"
        + '<div data-element-id="oob-box" style="position:absolute;left:154px;top:40px;width:1126px;height:700px;overflow:hidden">'
        '<div style="width:1126px;height:760px">越界且滚动溢出的内容</div></div>',
    )
    # 纯滚动溢出 24px，复刻测试报告中成品页第 2 页的 1126x605 vs 1126x629。
    scroll = _slide(
        "slide-3",
        _TITLE % "滚动溢出页"
        + '<div data-element-id="scroll-box" style="position:absolute;left:77px;top:60px;width:1126px;height:605px;overflow:hidden">'
        '<div style="width:1126px;height:629px">滚动溢出内容</div></div>',
    )
    return assemble_locked_template([clean, oob, scroll])


def leaf_label_deck(top=200):
    # 复刻真实复验中的典型 leaf 溢出：52px 小标签盒容纳 61px 内容。
    # 该缺陷 zoom 因子 0.844 低于 MIN_ZOOM，且缩字会触发 text_too_small
    # 硬门禁，确定性修复只能让盒体向空闲区域增长。
    return assemble_locked_template([_slide(
        "slide-1",
        _TITLE % "标签页"
        + f'<div data-element-id="kpi-label" style="position:absolute;left:77px;top:{top}px;width:400px;height:52px;overflow:hidden;font-size:16px;line-height:1.4">'
        '<div style="height:61px">一个会在 52px 盒子里溢出到 61px 的小标签内容</div></div>',
    )])


def nested_cascade_deck():
    """Reproduce the locked real-run leaf -> parent cascade geometries.

    Slide 1 has one normal-flow 63px leaf.  Its 67px content grows the leaf
    to 68px, consuming the parent's 3px tail slack and leaving exactly
    278px -> 280px ancestor scroll overflow.

    Slide 2 has two adjacent normal-flow leaves.  Both must grow (the first
    must not mistake the second for a fixed obstacle); together they consume
    3px of tail slack and leave the locked 575px -> 582px ancestor delta.
    """
    leaf = (
        '<div data-element-id="leaf-278" style="width:130px;height:63px;overflow:hidden;font-size:16px;line-height:1.4">'
        '<div style="width:130px;height:67px">63 到 67 像素的叶节点</div></div>'
    )
    first = _slide(
        "slide-1",
        _TITLE % "278 像素父容器"
        + '<div data-element-id="parent-278" style="position:absolute;left:64px;top:100px;width:1152px;height:278px;overflow:hidden">'
        '<div style="height:212px"></div>' + leaf + '</div>',
    )
    two_leaves = (
        '<div data-element-id="leaf-575-a" style="width:130px;height:63px;overflow:hidden;font-size:16px;line-height:1.4">'
        '<div style="width:130px;height:67px">第一个 63 到 67 像素叶节点</div></div>'
        '<div data-element-id="leaf-575-b" style="width:121px;height:63px;overflow:hidden;font-size:16px;line-height:1.4">'
        '<div style="width:121px;height:67px">第二个 63 到 67 像素叶节点</div></div>'
    )
    second = _slide(
        "slide-2",
        _TITLE % "575 像素父容器"
        + '<div data-element-id="parent-575" style="position:absolute;left:64px;top:80px;width:1152px;height:575px;overflow:hidden">'
        '<div style="height:446px"></div>' + two_leaves + '</div>',
    )
    return assemble_locked_template([first, second])


class OverflowAutofitBrowserGate(unittest.TestCase):
    def test_autofit_clears_geometric_blockers_and_reinspects_green(self):
        html = overflowing_deck()
        validate_html(html, SLIDE_IDS)
        inspector = ChromiumDeckInspector()
        before = inspector.inspect(html, SLIDE_IDS)
        self.assertTrue(before["available"])
        self.assertFalse(before["passed"])
        codes = {issue["code"] for issue in before["issues"]}
        self.assertIn("content_out_of_bounds", codes)
        self.assertIn("element_scroll_overflow", codes)

        result = fit_deck_html(html, max_rounds=2)

        self.assertTrue(result["available"])
        self.assertTrue(result["rules"])
        self.assertTrue(result["converged"], result["remaining"])
        self.assertEqual(result["remaining"], [])
        # 注入的修复样式只使用白名单属性，必须仍通过模板校验。
        validate_html(result["html"], SLIDE_IDS)
        after = inspector.inspect(result["html"], SLIDE_IDS)
        self.assertTrue(after["available"])
        self.assertTrue(after["passed"], after["issues"])
        self.assertEqual(after["issues"], [])

    def test_autofit_is_idempotent_on_an_already_fitted_deck(self):
        first = fit_deck_html(overflowing_deck(), max_rounds=2)
        self.assertTrue(first["converged"], first["remaining"])

        # 修复块会被剥离后从原始测量确定性重建：同一输入必须收敛到同一
        # 规则集与同一份 HTML（幂等不动点），而不是叠加新规则。
        second = fit_deck_html(first["html"], max_rounds=2)

        self.assertTrue(second["available"])
        self.assertEqual(second["rules"], first["rules"])
        self.assertEqual(second["html"], first["html"])
        self.assertTrue(second["converged"])
        self.assertEqual(second["remaining"], [])

    def test_leaf_label_overflow_grows_into_slack_and_converges(self):
        html = leaf_label_deck()
        validate_html(html, ["slide-1"])
        inspector = ChromiumDeckInspector()
        before = inspector.inspect(html, ["slide-1"])
        self.assertTrue(before["available"])
        self.assertFalse(before["passed"])
        issue = next(item for item in before["issues"] if item["code"] == "element_scroll_overflow")
        # 失败诊断必须携带 slide、selector 与 scroll/client 几何。
        self.assertEqual(issue["slide_id"], "slide-1")
        self.assertIn('[data-element-id="kpi-label"]', issue["selector"])
        self.assertEqual(issue["geometry"]["client_height"], 52)
        self.assertEqual(issue["geometry"]["scroll_height"], 61)

        result = fit_deck_html(html, max_rounds=2)

        self.assertTrue(result["available"])
        self.assertTrue(result["converged"], result["remaining"])
        self.assertEqual(result["remaining"], [])
        rule = result["rules"][issue["selector"]]
        # leaf 修复必须是盒体增长，绝不缩放字号。
        self.assertIn("height: 62.00px !important", rule)
        self.assertNotIn("zoom", rule)
        validate_html(result["html"], ["slide-1"])
        after = inspector.inspect(result["html"], ["slide-1"])
        self.assertTrue(after["passed"], after["issues"])
        self.assertEqual(after["issues"], [])

        # 幂等：剥离后从原始测量确定性重建同一规则集与同一份 HTML。
        second = fit_deck_html(result["html"], max_rounds=2)
        self.assertEqual(second["rules"], result["rules"])
        self.assertEqual(second["html"], result["html"])
        self.assertTrue(second["converged"])

    def test_leaf_overflow_without_slack_stays_unfixable(self):
        # 贴底元素向下没有增长空间（top 668 + 52 == 720），zoom 又低于下限：
        # 确定性路径必须拒绝硬修，保持 fail-closed 交给 LLM/人工修复。
        html = leaf_label_deck(top=668)
        validate_html(html, ["slide-1"])

        result = fit_deck_html(html, max_rounds=2)

        self.assertTrue(result["available"])
        self.assertFalse(result["converged"])
        self.assertIn("zoom_below_floor", {item["reason"] for item in result["remaining"]})
        self.assertFalse(ChromiumDeckInspector().inspect(result["html"], ["slide-1"])["passed"])

    def test_nested_leaf_growth_propagates_to_ancestors_using_flow_sibling_slack(self):
        html = nested_cascade_deck()
        slide_ids = ["slide-1", "slide-2"]
        validate_html(html, slide_ids)
        inspector = ChromiumDeckInspector()

        before = inspector.inspect(html, slide_ids)
        self.assertTrue(before["available"])
        leaf_issues = [item for item in before["issues"] if item["code"] == "element_scroll_overflow"]
        self.assertEqual(len(leaf_issues), 3, before["issues"])
        self.assertEqual(
            {(item["geometry"]["client_height"], item["geometry"]["scroll_height"]) for item in leaf_issues},
            {(63, 67)},
        )

        # One repair round may only grow deepest leaves.  Chromium is then
        # re-measured and must expose the exact real-run ancestor deltas.
        leaf_round = fit_deck_html(html, max_rounds=1)
        self.assertFalse(leaf_round["converged"])
        self.assertEqual(len(leaf_round["rules"]), 3)
        self.assertTrue(all("height: 68.00px !important" in rule for rule in leaf_round["rules"].values()))
        self.assertTrue(all("zoom" not in rule for rule in leaf_round["rules"].values()))
        after_leaf = inspector.inspect(leaf_round["html"], slide_ids)
        parent_issues = [item for item in after_leaf["issues"] if item["code"] == "element_scroll_overflow"]
        self.assertEqual(
            {
                (item["geometry"]["client_height"], item["geometry"]["scroll_height"])
                for item in parent_issues
            },
            {(278, 280), (575, 582)},
            parent_issues,
        )

        result = fit_deck_html(html, max_rounds=2)

        self.assertTrue(result["available"])
        self.assertTrue(result["converged"], result["remaining"])
        self.assertEqual(result["remaining"], [])
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(len(result["rules"]), 5)
        leaf_selectors = [selector for selector in result["rules"] if "leaf-" in selector]
        parent_selectors = [selector for selector in result["rules"] if "parent-" in selector]
        self.assertEqual(len(leaf_selectors), 3)
        self.assertEqual(len(parent_selectors), 2)
        self.assertLess(
            max(result["html"].index(selector) for selector in leaf_selectors),
            min(result["html"].index(selector) for selector in parent_selectors),
        )
        self.assertTrue(all("zoom" not in result["rules"][selector] for selector in leaf_selectors + parent_selectors))
        validate_html(result["html"], slide_ids)
        after = inspector.inspect(result["html"], slide_ids)
        self.assertTrue(after["passed"], after["issues"])
        self.assertEqual(after["issues"], [])

        # Rebuilding from the fitted input must reach the same fixed point.
        second = fit_deck_html(result["html"], max_rounds=2)
        self.assertEqual(second["rules"], result["rules"])
        self.assertEqual(second["html"], result["html"])
        self.assertTrue(second["converged"])

    def test_unfixable_overflow_stays_on_the_remaining_list(self):
        # 需要缩放到 0.5 以下才能放行的目标低于 MIN_ZOOM 下限，确定性路径
        # 必须拒绝硬缩并把它留给 LLM/人工修复，而不是假装已绿。
        fragment = _slide(
            "slide-1",
            _TITLE % "极限溢出"
            + '<div data-element-id="huge-box" style="position:absolute;left:0px;top:0px;width:2560px;height:1440px;overflow:hidden">'
            '<div style="width:2560px;height:1440px">巨幅内容</div></div>',
        )
        html = assemble_locked_template([fragment])
        validate_html(html, ["slide-1"])

        result = fit_deck_html(html, max_rounds=2)

        self.assertTrue(result["available"])
        self.assertFalse(result["converged"])
        self.assertTrue(result["remaining"])
        self.assertEqual(result["remaining"][0]["reason"], "zoom_below_floor")
        self.assertFalse(ChromiumDeckInspector().inspect(result["html"], ["slide-1"])["passed"])


if __name__ == "__main__":
    unittest.main()
