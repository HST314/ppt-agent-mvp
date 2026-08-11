"""AC-07: the real API/page journey creates two adjustable HTML samples."""

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
        rendered = page.decode()
        self.assertTrue(status.startswith("200"))
        self.assertIn('<iframe sandbox=""', rendered)
        self.assertIn("提交修改", rendered)
        self.assertIn("确认样品并生成全稿", rendered)


if __name__ == "__main__":
    import unittest
    unittest.main()
