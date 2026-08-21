import unittest
from unittest import mock

from ppt_agent.browser_inspection import ChromiumDeckInspector
from ppt_agent.p4 import assemble_locked_template, render, validate_html


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


if __name__ == "__main__":
    unittest.main()
