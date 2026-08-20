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


if __name__ == "__main__":
    unittest.main()
