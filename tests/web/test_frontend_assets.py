import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


class FrontendAssetTests(unittest.TestCase):
    def test_app_shell_has_external_modules_and_accessibility_landmarks(self):
        html = (FRONTEND / "index.html").read_text()
        self.assertIn('lang="zh-CN"', html)
        self.assertIn('class="skip-link"', html)
        self.assertIn('type="module"', html)
        self.assertNotRegex(html, r"<script(?![^>]+src=)[^>]*>")
        self.assertIn('aria-live="polite"', html)

    def test_required_modules_components_and_design_tokens_exist(self):
        required = [
            "static/js/app.js", "static/js/api.js", "static/js/router.js", "static/js/store.js",
            "static/js/job-tracker.js", "static/js/shell.js", "static/js/components/index.js",
            "static/js/stages/index.js", "static/js/stages/input.js", "static/js/stages/planning.js",
            "static/js/stages/sample.js", "static/js/stages/deck.js", "static/js/stages/review.js",
            "static/js/stages/delivery.js", "static/js/stages/shared.js",
            "static/css/tokens.css", "static/css/base.css", "static/css/layout.css",
            "static/css/components.css", "static/css/stages.css",
        ]
        for relative in required:
            self.assertTrue((FRONTEND / relative).is_file(), relative)
        tokens = (FRONTEND / "static/css/tokens.css").read_text()
        for token in ("--color-primary", "--color-accent", "--color-destructive", "[data-theme=\"dark\"]"):
            self.assertIn(token, tokens)
        css = "\n".join(path.read_text() for path in (FRONTEND / "static/css").glob("*.css"))
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("max-width: 1023px", css)
        self.assertIn("max-width: 767px", css)
        self.assertNotIn("overflow-x: auto;", css)

        app = (FRONTEND / "static/js/app.js").read_text()
        self.assertNotIn("/legacy/", app)
        self.assertNotIn("兼容阶段界面", app)

    def test_untrusted_content_is_not_assigned_to_inner_html(self):
        javascript = "\n".join(path.read_text() for path in (FRONTEND / "static/js").rglob("*.js"))
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("outerHTML", javascript)
        self.assertNotRegex(javascript, re.compile(r"insertAdjacentHTML", re.I))
        self.assertIn("fingerprint(stableStringify(payload))", javascript)


if __name__ == "__main__":
    unittest.main()
