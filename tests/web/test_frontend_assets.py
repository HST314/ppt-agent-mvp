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
        self.assertIn("timeout: recheck ? 90_000 : 8_000", api)
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
        self.assertIn("waiting_for_runtime", input_stage)
        self.assertIn("继续生成澄清问题", input_stage)
        self.assertIn("模型恢复探测中", app)
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
        self.assertIn("maxTerminalReconcileAttempts", tracker)
        self.assertIn("onTerminalReconcile", tracker + app)
        self.assertIn("authoritativeReferenceReached", app)
        self.assertIn("readAuthoritativeStageView", app)
        self.assertIn("renderRoute(route, authority)", app)
        self.assertIn("const route = currentRoute();", app.split("onComplete: async", 1)[1])
        self.assertNotIn("renderRoute(route);", app.split("onComplete: async", 1)[1].split("});", 1)[0])
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
        for token in ("Agent 步数", "模型请求", "Skill 工具调用", "执行详情", "技术审计", "jobEventHistory", "jobAgentAudits"):
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

    def test_auto_enqueue_entries_are_version_gated_before_dispatch(self):
        js = lambda name: (FRONTEND / "static/js" / name).read_text()
        app = js("app.js")
        shell = js("shell.js")
        shared = js("stages/shared.js")
        sample = js("stages/sample.js")
        input_stage = js("stages/input.js")
        delivery = js("stages/delivery.js")
        # 派发前二次校验 mismatch：所有 Job 创建入口（含 requiresRuntime=false 的
        # delivery.publish）无条件先过版本门禁，模型就绪校验仅限运行时操作。
        self.assertIn("ensureVersionMatchAllowed", app)
        self.assertIn("await ensureVersionMatchAllowed();", app.split("async function ensureRuntimeActionAllowed", 1)[1])
        tracked = app.split("async function startTrackedJob", 1)[1].split("const job = await create();", 1)[0]
        self.assertIn("await ensureVersionMatchAllowed();", tracked)
        self.assertIn("if (requiresRuntime) assertRuntimeReady();", tracked)
        self.assertNotIn("if (requiresRuntime) await ensureRuntimeActionAllowed();", tracked)
        self.assertIn("runtimeVersionMismatch()", app.split("async function ensureVersionMatchAllowed", 1)[1])
        # 版本阻断状态独立于 disabled 当前值：mismatch 期间始终打标，
        # 解除时只恢复版本门禁自己禁用的控件，不误改业务禁用态。
        version_gate = app.split("querySelectorAll('[data-requires-version-match", 1)[1]
        self.assertIn('control.dataset.versionDisabled = "true"', version_gate)
        self.assertNotIn('if (!control.disabled) control.dataset.versionDisabled = "true"', version_gate)
        self.assertIn("versionPrevEnabled", version_gate)
        self.assertIn('document.dispatchEvent(new CustomEvent("versiongatechange"))', app)
        self.assertIn('document.addEventListener("versiongatechange", updateGate', input_stage)
        # runAction 自动入队入口经统一版本守卫二次校验；按钮层用独立的版本门禁，
        # 模型不可用但版本一致时不得误伤（后端对该场景有降级路径）。
        self.assertIn("setVersionMatchGuard", app + shared)
        self.assertIn("if (requiresVersionMatch && versionMatchGuard) await versionMatchGuard();", shared)
        self.assertIn('"data-requires-version-match": options.requiresVersionMatch ? "true" : null', shell)
        self.assertIn("[data-requires-version-match=\"true\"]", app)
        # 样品确认（自动入队 deck.generate）：按钮与派发双重门禁。
        confirm_block = sample.split("确认当前样品并进入全稿", 1)[1].split("goTo", 1)[0]
        self.assertIn("requiresVersionMatch: true", confirm_block)
        # 导入/重建（自动入队 clarification.generate）：按钮门禁且重建闸不与版本门禁互相覆盖。
        self.assertIn('"重建资料快照" : "导入并冻结资料", { kind: "primary", type: "submit", mutates: true, requiresVersionMatch: true }', input_stage)
        self.assertIn('submit.dataset.versionDisabled !== "true"', input_stage)
        import_action = input_stage.split("api.importInput", 1)[1].split("});", 1)[0]
        self.assertIn("requiresVersionMatch: true", import_action)
        # 提交答案（可自动入队下一轮澄清）：按钮与派发双重门禁。
        self.assertIn('"提交答案并继续", { kind: "primary", type: "submit", mutates: true, requiresVersionMatch: true }', input_stage)
        answers_action = input_stage.split("api.answerClarifications", 1)[1].split("});", 1)[0]
        self.assertIn("requiresVersionMatch: true", answers_action)
        # 交付发布（delivery.publish）：按钮层补版本门禁。
        publish_block = delivery.split("将离线包写入工程文件夹", 1)[1].split("});", 1)[0]
        self.assertIn("requiresVersionMatch: true", publish_block)
        # 忙碌恢复不得解除版本门禁的独立阻断状态。
        components = js("components/index.js")
        self.assertIn('buttonNode.disabled = buttonNode.dataset.versionDisabled === "true";', components)


if __name__ == "__main__":
    unittest.main()
