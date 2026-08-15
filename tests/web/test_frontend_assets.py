import re
import subprocess
import sys
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

        input_stage = (FRONTEND / "static/js/stages/input.js").read_text()
        self.assertIn("没有检测到图片资源", input_stage)
        self.assertIn("继续无图片", input_stage)
        self.assertIn("返回准备资源", input_stage)
        self.assertIn("sessionStorage", input_stage)

        retired = (ROOT / "ppt_agent" / "api.py").read_text()
        self.assertNotIn("<html", retired)
        self.assertNotIn("<style", retired)
        self.assertNotIn("<script", retired)
        self.assertIn("create_app", retired)

    def test_every_module_import_and_entry_asset_has_the_same_build_key(self):
        index = (FRONTEND / "index.html").read_text()
        build = re.search(r'<meta name="app-build" content="([^"]+)"', index).group(1)
        self.assertTrue(build)
        self.assertEqual(set(re.findall(r'[?&]v=([^"\']+)', index)), {build})
        for module in (FRONTEND / "static/js").rglob("*.js"):
            source = module.read_text()
            imports = re.findall(r'(?:from\s+|import\()["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', source)
            for specifier in imports:
                with self.subTest(module=module.name, specifier=specifier):
                    self.assertTrue(specifier.endswith(f"?v={build}"))

    def test_frontend_build_key_is_fresh_for_current_asset_content(self):
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/update_frontend_build.py"), "--check"],
            cwd=ROOT,
            check=True,
        )

    def test_runtime_status_is_server_backed_and_recovery_is_actionable(self):
        app = (FRONTEND / "static/js/app.js").read_text()
        api = (FRONTEND / "static/js/api.js").read_text()
        input_stage = (FRONTEND / "static/js/stages/input.js").read_text()
        self.assertIn('fetch("/livez"', api)
        self.assertIn('fetch(recheck ? "/v1/runtime/recheck" : "/v1/runtime/status"', api)
        for label in ("浏览器在线", "后端可达", "模型可用", "模型不可用"):
            self.assertIn(label, app)
        self.assertIn('role: "status"', app)
        self.assertIn("runtimeSignature", app)
        self.assertIn("模型能力探测详情", app)
        self.assertIn("失败检查", app)
        self.assertIn("探测 ID", app)
        self.assertIn("runtimeProbes", api)
        self.assertIn('data-requires-runtime="true"', app)
        self.assertIn("model_authentication_failed", input_stage)
        self.assertIn("model_rate_limited", input_stage)
        self.assertIn("model_upstream_unavailable", input_stage)
        self.assertIn("Agent 审计 ID", input_stage)
        self.assertIn("复制探测 ID", input_stage)
        self.assertIn('role: "alert"', input_stage)

    def test_untrusted_content_is_not_assigned_to_inner_html(self):
        javascript = "\n".join(path.read_text() for path in (FRONTEND / "static/js").rglob("*.js"))
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("outerHTML", javascript)
        self.assertNotRegex(javascript, re.compile(r"insertAdjacentHTML", re.I))
        self.assertIn("fingerprint(stableStringify(payload))", javascript)

    def test_job_transport_and_intent_recovery_are_bounded_and_persistent(self):
        tracker = (FRONTEND / "static/js/job-tracker.js").read_text()
        store = (FRONTEND / "static/js/store.js").read_text()
        app = (FRONTEND / "static/js/app.js").read_text()
        self.assertIn("maxStreamFailures", tracker)
        self.assertIn("maxRecoveryAttempts", tracker)
        self.assertIn("scheduleRecovery", tracker)
        self.assertIn("events?after=${track.seq}", tracker)
        self.assertIn("ppt-agent:job-intent:", store)
        self.assertIn("bindJobIntent(job, intent.storageKey)", app)
        self.assertIn("reconcileStoredIntents", app)


if __name__ == "__main__":
    unittest.main()
