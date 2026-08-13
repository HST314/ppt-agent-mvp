"""AC-07: the real API/page journey creates two adjustable HTML samples."""

from pathlib import Path

from .support import SampleJourney


class AC07SamplesE2E(SampleJourney):
    def test_default_two_real_html_samples_reach_sandboxed_page(self):
        generated = self.ok("/v1/tasks/journey/samples/generate", {})
        self.assertEqual(len(generated["selection"]["slide_ids"]), 2)
        html = generated["sample"]["html"]
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertEqual(html.count('data-slide-id="'), 2)
        self.assertIn('src="data:image/png;base64,', html)

        status, page = self.call("GET", "/tasks/journey/samples")
        self.assertTrue(status.startswith("200"))
        self.assertIn('type="module"', page.decode())
        module = Path("frontend/static/js/stages/sample.js").read_text()
        self.assertIn("previewFrame", module)
        self.assertIn("提交样品修改", module)
        self.assertIn("确认当前样品并进入全稿", module)
        preview_status, preview = self.call("GET", f'/v1/tasks/journey/previews/{generated["sample"]["hash"]}')
        self.assertTrue(preview_status.startswith("200"))
        self.assertIn(b'data-slide-id="', preview)


if __name__ == "__main__":
    import unittest
    unittest.main()
