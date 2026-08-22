import unittest
from unittest import mock

from ppt_agent.browser_inspection import ChromiumDeckInspector
from ppt_agent.p4 import assemble_locked_template, locked_template, render, validate_html


class P0HybridInspectionBrowserGate(unittest.TestCase):
    def test_locked_template_passes_real_chromium_geometry(self):
        html = render("## [slide-1] 结论\n- 核心内容", ["slide-1"])
        validate_html(html, ["slide-1"])

        evidence = ChromiumDeckInspector().inspect(html, ["slide-1"])

        self.assertTrue(evidence["available"])
        self.assertTrue(evidence["passed"], evidence["issues"])
        self.assertEqual(evidence["slides"][0]["width"], 1280)
        self.assertEqual(evidence["slides"][0]["height"], 720)

    def test_real_chromium_overflow_cannot_report_green(self):
        fragment = (
            '<section class="slide light" id="slide-1" data-slide-id="slide-1">'
            '<h1 data-element-id="title">溢出回归</h1>'
            '<div data-element-id="overflow" style="position:absolute;left:1240px;top:160px;width:180px;height:80px">越界内容</div>'
            '</section>'
        )
        html = assemble_locked_template([fragment])
        validate_html(html, ["slide-1"])

        evidence = ChromiumDeckInspector().inspect(html, ["slide-1"])

        self.assertTrue(evidence["available"])
        self.assertFalse(evidence["passed"])
        issue = next(item for item in evidence["issues"] if item["code"] == "content_out_of_bounds")
        self.assertEqual(issue["slide_id"], "slide-1")
        self.assertEqual(issue["element_id"], "overflow")

    def test_missing_browser_fails_closed_instead_of_fake_green(self):
        inspector = ChromiumDeckInspector()
        with mock.patch.object(inspector, "_measure", side_effect=RuntimeError("browser absent")):
            evidence = inspector.inspect("<!doctype html>", ["slide-1"])

        self.assertFalse(evidence["available"])
        self.assertFalse(evidence["passed"])
        self.assertEqual(evidence["issues"][0]["code"], "render_unavailable")

    def test_nested_overflow_is_deduped_to_root_cause(self):
        fragment = (
            '<section class="slide light" id="slide-1" data-slide-id="slide-1">'
            '<h1 data-element-id="title">嵌套越界</h1>'
            '<div data-element-id="root-box" style="position:absolute;left:1100px;top:80px;width:400px;height:560px">'
            '<div data-element-id="child-list" style="width:380px;height:520px">'
            '<p data-element-id="child-item">子项内容</p>'
            '</div></div>'
            '</section>'
        )
        html = assemble_locked_template([fragment])
        validate_html(html, ["slide-1"])

        evidence = ChromiumDeckInspector().inspect(html, ["slide-1"])

        issues = [item for item in evidence["issues"] if item["code"] == "content_out_of_bounds"]
        self.assertEqual(len(issues), 1, evidence["issues"])
        self.assertEqual(issues[0]["element_id"], "root-box")
        self.assertIn("child-list", issues[0]["evidence"])
        self.assertIn("child-item", issues[0]["evidence"])

    def test_meta_role_text_has_independent_threshold(self):
        fragment = (
            '<section class="slide light" id="slide-1" data-slide-id="slide-1">'
            '<h1 data-element-id="title">角色阈值</h1>'
            '<p data-element-id="kicker" style="font-size:13px">装饰性 KICKER</p>'
            '<p data-element-id="body-copy" style="font-size:13px">正文十三像素</p>'
            '<small data-element-id="footnote" style="font-size:10px">脚注说明文字</small>'
            '</section>'
        )
        html = assemble_locked_template([fragment])
        validate_html(html, ["slide-1"])

        evidence = ChromiumDeckInspector().inspect(html, ["slide-1"])

        small = {item["element_id"]: item for item in evidence["issues"] if item["code"] == "text_too_small"}
        self.assertIn("body-copy", small)
        self.assertIn("minimum=16", small["body-copy"]["evidence"])
        self.assertNotIn("kicker", small)
        self.assertIn("footnote", small)
        self.assertIn("role=meta", small["footnote"]["evidence"])
        self.assertIn("minimum=12", small["footnote"]["evidence"])

    def test_visual_quality_captures_stable_webp_and_scores_composition_advisories(self):
        slides = "".join(
            f'<section class="slide light" data-slide-id="slide-{index}" data-layout="same">'
            f'<h1 data-element-id="title">Page {index}</h1><p data-element-id="body">tiny content</p></section>'
            for index in range(1, 5)
        )
        html = (
            '<!doctype html><html><head><style>*{box-sizing:border-box}body{margin:0}'
            '.slide{width:1280px;height:720px;position:relative;background:#fff;overflow:hidden}'
            'h1{position:absolute;left:40px;top:32px;font-size:40px}'
            'p{position:absolute;left:40px;top:100px;font-size:18px}'
            f'</style></head><body>{slides}</body></html>'
        )

        evidence = ChromiumDeckInspector().inspect(
            html,
            [f"slide-{index}" for index in range(1, 5)],
            visual_quality=True,
        )

        self.assertTrue(evidence["available"])
        self.assertLess(evidence["visual_quality"]["score"], 70)
        self.assertEqual(len(evidence["visual_quality"]["screenshots"]), 4)
        self.assertEqual(len(evidence["_visual_screenshots"]), 4)
        for declared, payload in zip(evidence["visual_quality"]["screenshots"], evidence["_visual_screenshots"]):
            self.assertEqual(declared["media_type"], "image/webp")
            self.assertEqual(declared["byte_size"], len(payload["content"]))
            self.assertEqual(payload["content"][:4], b"RIFF")
            self.assertEqual(payload["content"][8:12], b"WEBP")
        codes = {item["code"] for item in evidence["issues"]}
        self.assertTrue({"excessive_whitespace", "visual_imbalance", "repetitive_layout", "flat_theme_rhythm"}.issubset(codes))
        self.assertTrue(all(item["severity"] == "warning" for item in evidence["issues"]))
        self.assertTrue(evidence["passed"], "advisory warnings must not fail the technical hard gate")

    def test_undefined_layout_class_is_blocked_from_real_cssom_evidence(self):
        fragment = (
            '<section class="slide light" id="slide-1" data-slide-id="slide-1" data-layout="S11">'
            '<h1 data-element-id="title">真实 CSSOM 门禁</h1>'
            '<div class="ghost-layout-section"><div class="ghost-layout">阶段一</div></div>'
            '</section>'
        )
        html = f'<!doctype html><html><head><style>{locked_template("swiss")["style"]}</style></head><body>{fragment}</body></html>'
        validate_html(html, ["slide-1"])

        evidence = ChromiumDeckInspector().inspect(html, ["slide-1"])

        self.assertTrue(evidence["available"])
        self.assertFalse(evidence["passed"])
        undefined = {item["evidence"] for item in evidence["issues"] if item["code"] == "undefined_layout_class"}
        self.assertTrue(any("class=.ghost-layout-section" in item for item in undefined), evidence["issues"])
        self.assertTrue(any("matched_css_rule=0" in item for item in undefined))

    def test_ancestor_semantic_class_defined_by_real_descendant_rule_passes(self):
        fragment = (
            '<section class="slide split" id="slide-1" data-slide-id="slide-1" data-layout="S10">'
            '<div class="canvas-card"><div class="split-half">'
            '<div class="half"><h1 data-element-id="title">S10 左侧</h1></div>'
            '<div class="half"><p>右侧说明</p></div>'
            '</div></div></section>'
        )
        html = f'<!doctype html><html><head><style>{locked_template("swiss")["style"]}</style></head><body>{fragment}</body></html>'

        evidence = ChromiumDeckInspector().inspect(html, ["slide-1"])

        self.assertTrue(evidence["available"], evidence["issues"])
        undefined = [item for item in evidence["issues"] if item["code"] == "undefined_layout_class"]
        self.assertFalse(any("class=.split" in item["evidence"] for item in undefined), evidence["issues"])

    def test_ancestor_class_without_a_real_subtree_match_remains_blocked(self):
        fragment = (
            '<section class="slide missing-class" id="slide-1" data-slide-id="slide-1" data-layout="S10">'
            '<h1 data-element-id="title">缺失后代</h1><p>真实内容</p></section>'
        )
        html = (
            '<!doctype html><html><head><style>'
            f'{locked_template("swiss")["style"]}'
            '.slide.missing-class .required-descendant{display:grid}'
            '</style></head><body>' + fragment + '</body></html>'
        )

        evidence = ChromiumDeckInspector().inspect(html, ["slide-1"])

        undefined = [item for item in evidence["issues"] if item["code"] == "undefined_layout_class"]
        self.assertTrue(any("class=.missing-class" in item["evidence"] for item in undefined), evidence["issues"])

    def test_swiss_body_zh_is_a_real_locked_cssom_class(self):
        fragment = (
            '<section class="slide light" id="slide-1" data-slide-id="slide-1" data-layout="S07">'
            '<h1 data-element-id="title">正文词汇一致性</h1>'
            '<p class="body-zh" data-element-id="body">这段中文正文必须命中 Swiss 锁定 CSS 规则。</p>'
            '</section>'
        )
        html = f'<!doctype html><html><head><style>{locked_template("swiss")["style"]}</style></head><body>{fragment}</body></html>'

        evidence = ChromiumDeckInspector().inspect(html, ["slide-1"])

        self.assertTrue(evidence["available"], evidence["issues"])
        undefined = [item for item in evidence["issues"] if item["code"] == "undefined_layout_class"]
        self.assertFalse(undefined, evidence["issues"])

    def test_editorial_hero_dark_semantics_are_backed_by_real_css_rules(self):
        fragment = (
            '<section class="slide hero dark" id="slide-1" data-slide-id="slide-1" data-layout="A01">'
            '<h1 data-element-id="title">运营复盘</h1><p class="body-zh">决策摘要</p></section>'
        )
        html = f'<!doctype html><html><head><meta name="ppt-semantic-classes" content="light dark hero"><style>{locked_template("editorial")["style"]}</style></head><body>{fragment}</body></html>'

        evidence = ChromiumDeckInspector().inspect(html, ["slide-1"])

        undefined = [item for item in evidence["issues"] if item["code"] == "undefined_layout_class"]
        self.assertFalse(undefined, evidence["issues"])
        self.assertIn("hero", evidence["slides"][0]["layout_validation"]["registered_semantic_classes"])


if __name__ == "__main__":
    unittest.main()
