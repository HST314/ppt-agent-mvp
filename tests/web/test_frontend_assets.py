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
        body_rule = re.search(r"^body\s*\{(?P<body>.*?)\}", css, re.S | re.M).group("body")
        self.assertIn("overflow-x: hidden", body_rule)
        self.assertIn(".progress-rail", css)
        self.assertIn("overflow-x: auto", css)

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
        self.assertIn('runtimeFetch("/livez"', api)
        self.assertIn('runtimeFetch(recheck ? "/v1/runtime/recheck" : "/v1/runtime/status"', api)
        self.assertIn("runtimeFetch", api)
        self.assertIn("backendReachable: true, runtimeReady: null", api)
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
        for label in ("失败阶段", "工具调用数", "底层错误"):
            self.assertIn(label, app + input_stage)
        for code in ("probe_tool_call_missing", "probe_tool_round_failed", "probe_tool_final_invalid_output"):
            self.assertIn(code, input_stage)

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
        self.assertIn("maxPollInterval", tracker)
        self.assertIn("pollFailures", tracker)
        self.assertIn("scheduleRecovery", tracker)
        self.assertIn("events?after=${track.seq}", tracker)
        self.assertIn("ppt-agent:job-intent:", store)
        self.assertIn("bindJobIntent(job, intent.storageKey)", app)
        self.assertIn("reconcileStoredIntents", app)
        self.assertIn("latestJobFailure", app)
        self.assertIn("前往本阶段操作区重试", app)
        heartbeat_branch = tracker.split('if (event.type === "heartbeat") {', 1)[1].split("return;", 1)[0]
        self.assertIn('onTransport?.("heartbeat"', heartbeat_branch)
        self.assertNotIn("onEvent", heartbeat_branch)
        for token in ("job-panel__business-step", "job-panel__elapsed", "job-panel__deadline", "job-panel__transport", "job-panel__cancel-feedback"):
            self.assertIn(token, app)
        for token in ("Agent 步数", "模型请求", "只读工具调用", "执行详情", "技术审计", "jobEventHistory", "jobAgentAudits"):
            self.assertIn(token, app + (FRONTEND / "static/js/api.js").read_text())
        self.assertIn("`${event.job_id}:${event.seq}`", app)
        self.assertIn('"storage-error"', tracker + app)
        self.assertIn('"aria-live": "polite"', app)
        self.assertIn("event_history_warning", app)

    def test_frontend_backend_version_mismatch_prompts_restart(self):
        app = (FRONTEND / "static/js/app.js").read_text()
        css = "\n".join(path.read_text() for path in (FRONTEND / "static/css").glob("*.css"))
        # 校验信号来自后端就绪载荷：backend_commit 与 frontend_build 必须参与比较。
        self.assertIn("runtimeState.health?.frontend_build", app)
        self.assertIn("backend_commit", app)
        self.assertIn("runtimeVersionMismatch", app)
        # 不一致时必须明确提示重启：顶栏徽标、页面横幅与禁用原因三层提示。
        self.assertIn("版本不一致·需重启", app)
        self.assertIn("前端与后端版本不一致，请重启后端服务", app)
        self.assertIn('"data-version-mismatch-banner"', app)
        self.assertIn('role: "alert"', app)
        self.assertIn("前端与后端版本不一致，请先重启后端服务", app)
        self.assertIn("python -m uvicorn main_front:app", app)
        self.assertIn("git rev-parse HEAD", app)
        # 设置页展示完整版本与提交信息，并对未知 commit 给出说明。
        self.assertIn('"data-runtime-version-details"', app)
        for label in ("前端 Build", "后端 Build", "后端 commit", "前后端版本一致", "无法校验代码版本"):
            self.assertIn(label, app)
        self.assertIn(".version-banner", css)
        self.assertIn(".version-warning", css)


if __name__ == "__main__":
    unittest.main()
