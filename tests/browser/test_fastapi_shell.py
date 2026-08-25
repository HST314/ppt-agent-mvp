"""FastAPI application shell browser, responsive and accessibility gate."""
import base64
import socket
import tempfile
import threading
import time
import unittest

import uvicorn
from playwright.sync_api import sync_playwright

from ppt_agent.errors import GatewayError
from ppt_agent.p4 import assemble_locked_template
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app

PNG_1X1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class BrowserImageBuilder:
    version = "browser-image-builder"

    def build(self, _outline, **context):
        data_url = next(iter(context["assets"].values()))
        sections = []
        for index, slide_id in enumerate(context["slide_ids"]):
            images = ""
            if index == 0:
                images = (
                    f'<img id="data-image" src="{data_url}" alt="Base64">'
                    '<img id="relative-image" src="hero.png" alt="Relative">'
                    '<img id="remote-image" src="https://images.example/remote.png" alt="Remote">'
                    '<div id="css-image" style="width:20px;height:20px;background-image:url(hero.png)"></div>'
                )
            sections.append(
                f'<section class="slide" id="{slide_id}" data-slide-id="{slide_id}">'
                f'<h2>图片预览 {index + 1}</h2><p>验证图片资源解析。</p>{images}</section>'
            )
        return assemble_locked_template(
            sections,
            context.get("rules", []),
            context.get("design_contract"),
            context.get("design_contract_hash"),
        )


def model_question():
    return {
        "question_id": "business-decision",
        "field_path": "decision",
        "prompt": "本次发布需要管理层批准预算，还是仅同步项目进展？",
        "helper_text": "这会决定论证结构和数据深度。",
        "options": [
            {"value": "approve", "label": "批准预算", "description": "以决策材料为主"},
            {"value": "update", "label": "同步进展", "description": "以项目状态为主"},
        ],
        "allow_other": True,
        "blocking": True,
    }


class ControlledClarifier:
    model = "browser-clarifier"

    def __init__(self, *, blocked=False, failures=0):
        self.release = threading.Event()
        self.started = threading.Event()
        self.failures = failures
        if not blocked:
            self.release.set()

    def clarify(self, _payload):
        self.started.set()
        if not self.release.wait(15):
            raise RuntimeError("clarifier test timed out")
        if self.failures:
            self.failures -= 1
            raise RuntimeError("model gateway unavailable")
        return {"questions": [model_question()], "model": self.model}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FastAPIShellBrowserGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = TaskService(WorkspaceStore(self.tmp.name))
        port = free_port()
        self.app = create_app(self.service)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="critical")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started)
        self.base = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.tmp.cleanup()

    def new_page(self, width=1440, height=950, reduced_motion=None):
        page = self.browser.new_page(viewport={"width": width, "height": height}, reduced_motion=reduced_motion)
        errors = []
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        return page, errors

    def assert_no_page_overflow(self, page):
        self.assertTrue(page.locator("body").evaluate("node => node.scrollWidth <= node.clientWidth"))

    def test_create_shell_history_theme_and_keyboard(self):
        page, errors = self.new_page(1440)
        page.goto(self.base + "/")
        page.get_by_role("heading", name="从任务资料到可交付演示稿，都在一个工作台。").wait_for()
        self.assert_no_page_overflow(page)

        page.keyboard.press("Tab")
        self.assertEqual(page.locator(":focus").inner_text(), "跳到主要内容")
        page.get_by_role("button", name="打开设置").click()
        page.get_by_role("heading", name="显示与连接").wait_for()
        page.get_by_role("dialog").get_by_role("button", name="关闭").click()
        page.get_by_label("任务 ID").fill("browser-shell")
        page.get_by_label("运行模式").select_option("manual")
        page.get_by_role("button", name="创建任务并进入工作台").click()
        page.wait_for_url("**/tasks/browser-shell")
        page.get_by_role("heading", name="任务/资料").wait_for()
        self.assertFalse(page.locator(".menu-button").is_visible())
        self.assertFalse(page.locator(".sidebar__close").is_visible())
        self.assertEqual(page.locator(".progress-rail > li").count(), 8)
        self.assertEqual(page.locator('[aria-current="step"]').inner_text().splitlines()[0], "1")
        self.assertIn("前置条件", page.locator('.progress-node[aria-disabled="true"]').first.get_attribute("title"))

        page.goto(self.base + "/tasks/browser-shell/outline")
        page.wait_for_url("**/tasks/browser-shell?stage=outline")
        page.get_by_role("heading", name="逐页大纲").wait_for()
        page.go_back()
        page.get_by_role("heading", name="任务/资料").wait_for()

        theme_button = page.get_by_role("button", name="切换深色主题")
        theme_button.click()
        self.assertEqual(page.locator("html").get_attribute("data-theme"), "dark")
        self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).color"), "rgb(248, 245, 255)")
        self.assert_no_page_overflow(page)
        self.assertEqual(errors, [])
        page.close()

    def test_four_viewports_mobile_drawer_components_and_reduced_motion(self):
        self.service.create("responsive")
        for width, height in ((375, 820), (667, 375), (768, 820), (1024, 820), (1440, 820)):
            page, errors = self.new_page(width, height, "reduce")
            page.goto(self.base + "/tasks/responsive")
            page.get_by_role("heading", name="任务/资料").wait_for()
            self.assert_no_page_overflow(page)
            if width < 1024:
                menu = page.get_by_role("button", name="打开任务与阶段导航")
                menu.click()
                self.assertEqual(page.locator(".sidebar").get_attribute("data-open"), "true")
                self.assertGreaterEqual(page.locator(".task-list > li").count(), 1)
                page.keyboard.press("Escape")
                self.assertEqual(page.locator(".sidebar").get_attribute("data-open"), "false")
                self.assertTrue(menu.evaluate("node => document.activeElement === node"))
            duration = page.locator(".button").first.evaluate("node => getComputedStyle(node).transitionDuration")
            self.assertIn(duration, {"0s", "1e-05s", "0.00001s"})
            if width == 375:
                page.locator("html").evaluate("node => node.style.fontSize = '200%'")
                self.assert_no_page_overflow(page)
            self.assertEqual(errors, [])
            page.close()

        page, errors = self.new_page(375, 820)
        page.goto(self.base + "/components")
        page.get_by_role("heading", name="组件状态与可访问行为").wait_for()
        self.assert_no_page_overflow(page)
        page.get_by_role("button", name="打开确认对话框").click()
        self.assertTrue(page.get_by_role("dialog").is_visible())
        page.get_by_role("dialog").get_by_role("button", name="取消").click()
        self.assertEqual(errors, [])
        page.close()

    def test_storage_security_error_falls_back_without_breaking_shell(self):
        self.service.create("storage-denied")
        page, errors = self.new_page(375, 820)
        page.add_init_script("""
            for (const method of ['getItem', 'setItem', 'removeItem']) {
              Object.defineProperty(Storage.prototype, method, {
                configurable: true,
                value() { throw new DOMException('blocked by policy', 'SecurityError'); },
              });
            }
        """)
        page.goto(self.base + "/tasks/storage-denied")
        page.get_by_role("heading", name="任务/资料", exact=True).wait_for()
        page.get_by_role("button", name="打开任务与阶段导航").click()
        page.keyboard.press("Escape")
        page.get_by_role("button", name="切换深色主题").click()
        self.assertEqual(page.locator("html").get_attribute("data-theme"), "dark")
        self.assert_no_page_overflow(page)
        self.assertEqual(errors, [])
        page.close()

    def test_preview_decodes_base64_relative_remote_and_css_images(self):
        self.service.create("preview-images")
        self.service.store.put_resource("preview-images", "hero.png", PNG_1X1)
        self.service.import_input("preview-images", {"goal": "发布", "audience": "客户", "topic": "图片预览", "页数": 2})
        self.service.generate_narrative("preview-images")
        self.service.confirm_narrative("preview-images")
        self.service.generate_outline("preview-images")
        self.service.confirm_outline("preview-images")
        self.service.builder = BrowserImageBuilder()
        sample = self.service.generate_sample("preview-images")["sample"]

        page, errors = self.new_page()
        remote_requests = []
        page.route("https://images.example/remote.png", lambda route: (
            remote_requests.append(route.request.url),
            route.fulfill(status=200, content_type="image/png", body=PNG_1X1),
        )[-1])
        page.goto(f"{self.base}/v1/tasks/preview-images/previews/{sample['hash']}")
        page.locator("#remote-image").wait_for()
        for selector in ("#data-image", "#relative-image", "#remote-image"):
            self.assertTrue(page.locator(selector).evaluate("node => node.complete && node.naturalWidth === 1"))
        self.assertIn("preview-assets", page.locator("#css-image").evaluate("node => getComputedStyle(node).backgroundImage"))
        self.assertEqual(remote_requests, ["https://images.example/remote.png"])
        self.assertEqual(errors, [])
        page.close()

    def test_readiness_transport_failure_does_not_mislabel_live_backend(self):
        self.service.create("partial-health")
        page, errors = self.new_page()
        page.route("**/v1/runtime/status", lambda route: route.abort())

        page.goto(self.base + "/tasks/partial-health")
        page.get_by_role("heading", name="任务/资料").wait_for()
        page.get_by_text("后端可达", exact=True).first.wait_for()

        self.assertEqual(page.get_by_text("后端不可达", exact=True).count(), 0)
        self.assertTrue(page.get_by_text("模型：按任务调用", exact=True).first.is_visible())
        self.assertTrue(errors and all("ERR_FAILED" in error for error in errors))
        page.close()

    def test_failed_outline_job_details_survive_refresh_with_retry_entry(self):
        class FailingGenerator:
            model = "failing-browser-generator"
            def generate(self, *_args, **_kwargs):
                raise GatewayError("阶段工具契约连续不满足，生成已停止", code="stage_tool_contract_error")

        self.service.create("persistent-failure")
        self.service.import_input("persistent-failure", {"goal": "发布", "audience": "客户", "topic": "新品"})
        self.service.generate_narrative("persistent-failure")
        self.service.confirm_narrative("persistent-failure")
        self.service.generator = FailingGenerator()
        job, _ = self.app.state.job_service.create("persistent-failure", "outline.generate", {}, "failed-outline")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.app.state.job_service.get(job["job_id"])["status"] != "failed":
            time.sleep(0.01)
        self.assertEqual(self.app.state.job_service.get(job["job_id"])["status"], "failed")

        page, errors = self.new_page()
        page.goto(self.base + "/tasks/persistent-failure?stage=outline")
        page.get_by_role("heading", name="逐页大纲", exact=True).wait_for()
        failure = page.get_by_role("alert", name="最近一次后台任务失败")
        self.assertTrue(failure.get_by_text("阶段工具契约连续不满足，生成已停止", exact=True).is_visible())
        self.assertTrue(failure.get_by_text("失败类型：阶段工具契约错误", exact=True).is_visible())
        self.assertTrue(failure.get_by_text("错误代码：stage_tool_contract_error", exact=True).is_visible())
        self.assertTrue(failure.get_by_text("模型连续请求了本阶段不允许的工具或文件。系统已停止无效调用；请重试，若再次发生请提供诊断 ID。", exact=True).is_visible())
        self.assertTrue(failure.get_by_role("button", name="前往本阶段操作区重试").is_visible())

        page.reload()
        failure_after_reload = page.get_by_role("alert", name="最近一次后台任务失败")
        failure_after_reload.wait_for()
        self.assertTrue(failure_after_reload.is_visible())
        self.assertEqual(errors, [])
        page.close()

    def test_clarification_round_is_single_column_atomic_and_keyboard_accessible(self):
        self.service.create("clarification-round")
        imported = self.service.import_input("clarification-round", {"topic": "新品发布"})
        questions = imported["clarification"]["details"]
        page, errors = self.new_page(375, 820)
        submitted_requests = []
        page.on("request", lambda request: submitted_requests.append(request) if request.url.endswith("/clarifications/answers") else None)
        page.goto(self.base + "/tasks/clarification-round?stage=clarification")
        page.get_by_role("heading", name="需求澄清", exact=True).wait_for()

        form = page.locator(".clarification-form")
        cards = form.locator("fieldset.question-card")
        self.assertEqual(cards.count(), 2)
        self.assertEqual(form.locator("select").count(), 0)
        self.assertEqual(form.get_by_role("button", name="提交答案并继续", exact=True).count(), 1)
        self.assertEqual(form.locator('button[type="submit"]').count(), 1)
        self.assertEqual(form.get_by_text("已完成 0/2", exact=True).count(), 1)
        self.assertTrue(page.get_by_text("授权资源（辅助信息）", exact=True).is_visible())
        self.assertFalse(page.locator(".resource-disclosure").get_attribute("open"))
        self.assert_no_page_overflow(page)
        page.locator("html").evaluate("node => node.style.fontSize = '200%'")
        self.assert_no_page_overflow(page)
        page.locator("html").evaluate("node => node.style.fontSize = '100%'")

        form.get_by_role("button", name="提交答案并继续", exact=True).click()
        self.assertEqual(form.get_by_text("请回答此题后再提交本轮答案。", exact=True).count(), 2)
        self.assertTrue(cards.first.evaluate("node => document.activeElement === node"))
        self.assertEqual(submitted_requests, [])

        first_radio = cards.first.locator('input[type="radio"]').first
        first_radio.focus()
        page.keyboard.press("Space")
        self.assertTrue(first_radio.is_checked())
        self.assertEqual(form.get_by_text("已完成 1/2", exact=True).count(), 1)
        first_other = cards.first.get_by_label("也可以输入自己的答案")
        first_other.fill("促成采购审批")
        self.assertFalse(first_radio.is_checked())

        second_other = cards.nth(1).get_by_label("也可以输入自己的答案")
        second_other.fill("区域经销商")
        self.assertEqual(form.get_by_text("已完成 2/2", exact=True).count(), 1)
        form.get_by_role("button", name="提交答案并继续", exact=True).click()
        page.get_by_role("button", name="生成叙事结构", exact=True).wait_for()

        self.assertEqual(len(submitted_requests), 1)
        payload = submitted_requests[0].post_data_json["answers"]
        self.assertEqual(set(payload), {question["question_id"] for question in questions})
        self.assertEqual(payload[questions[0]["question_id"]], {"option": "Other", "other": "促成采购审批"})
        self.assertEqual(payload[questions[1]["question_id"]], {"option": "Other", "other": "区域经销商"})
        self.assertTrue(self.service.input_view("clarification-round")["clarification"]["confirmed"])
        self.assert_no_page_overflow(page)
        self.assertEqual(errors, [])
        page.close()

    def test_model_questions_stay_hidden_until_async_job_completes(self):
        clarifier = ControlledClarifier(blocked=True)
        self.service.clarifier = clarifier
        self.service.create("model-wait")
        page, errors = self.new_page(375, 820)
        page.goto(self.base + "/tasks/model-wait?stage=created")
        page.get_by_label("任务卡内容").fill("核心主题：新品发布")
        page.get_by_role("button", name="导入并冻结资料").click()

        page.get_by_role("heading", name="模型正在生成澄清问题").wait_for()
        self.assertTrue(clarifier.started.wait(2))
        self.assertEqual(page.locator("fieldset.question-card").count(), 0)
        self.assertEqual(page.get_by_text("无需额外澄清", exact=True).count(), 0)
        self.assertTrue(
            page.get_by_text(
                "可以安全离开此页；请求、校验或失败都会在 90 秒执行硬截止内形成明确结果，完成后工作台会自动刷新。",
                exact=True,
            ).is_visible()
        )
        self.assertEqual(page.get_by_role("button", name="取消后台任务").count(), 0)
        self.assert_no_page_overflow(page)

        clarifier.release.set()
        page.get_by_role("heading", name="需求澄清", exact=True).wait_for()
        self.assertEqual(page.locator("fieldset.question-card").count(), 1)
        self.assertTrue(page.get_by_text("AI 生成问题", exact=True).is_visible())
        self.assertTrue(page.get_by_text("本次发布需要管理层批准预算，还是仅同步项目进展？", exact=True).is_visible())
        self.assertEqual(errors, [])
        page.close()

    def test_model_failure_is_shown_on_job_and_fallback_remains_available(self):
        class FailingClarifier:
            model = "failing-browser-model"
            calls = 0
            def clarify(self, _payload):
                self.calls += 1
                raise GatewayError(
                    "模型服务认证失败，请联系管理员检查凭据",
                    code="model_authentication_failed",
                )

        clarifier = FailingClarifier()
        self.service.clarifier = clarifier
        self.service.create("runtime-failure")
        page, errors = self.new_page(375, 820, "reduce")
        page.goto(self.base + "/tasks/runtime-failure?stage=created")
        page.get_by_role("link", name="设置", exact=True).click()
        page.get_by_role("heading", name="任务与运行设置", exact=True).wait_for()
        page.get_by_role("tab", name="模型", exact=True).click()
        settings = page.locator("#settings-panel-models")
        settings.get_by_text("浏览器在线", exact=True).wait_for()
        settings.get_by_text("后端可达", exact=True).wait_for()
        settings.get_by_text("模型：按任务调用", exact=True).wait_for()
        page.get_by_role("link", name="工作区", exact=True).click()
        page.get_by_role("heading", name="任务/资料", exact=True).wait_for()

        page.get_by_label("任务卡内容").fill("核心主题：新品发布")
        page.get_by_role("button", name="导入并冻结资料").click()
        page.get_by_role("heading", name="问题生成失败").wait_for()
        retry = page.get_by_role("button", name="重新生成问题")
        self.assertFalse(retry.is_disabled())
        self.assertFalse(page.get_by_role("button", name="使用系统兜底问题").is_disabled())
        self.assertTrue(page.get_by_text("model_authentication_failed", exact=True).is_visible())
        self.assertTrue(page.get_by_text("请联系管理员修复模型凭据", exact=False).is_visible())

        page.get_by_role("button", name="使用系统兜底问题").click()
        page.get_by_role("dialog").get_by_role("button", name="确认使用兜底问题").click()
        page.get_by_role("heading", name="需求澄清", exact=True).wait_for()
        self.assertEqual(clarifier.calls, 1)
        self.assert_no_page_overflow(page)
        self.assertEqual(errors, [])
        page.close()

    def test_failed_model_can_retry_or_use_explicit_fallback(self):
        clarifier = ControlledClarifier(failures=1)
        self.service.clarifier = clarifier
        self.service.create("model-retry")
        page, errors = self.new_page()
        page.goto(self.base + "/tasks/model-retry?stage=created")
        page.get_by_label("任务卡内容").fill("核心主题：新品发布")
        page.get_by_role("button", name="导入并冻结资料").click()
        page.get_by_role("heading", name="问题生成失败").wait_for()
        self.assertEqual(page.locator("fieldset.question-card").count(), 0)
        self.assertTrue(page.get_by_text("澄清问题生成失败", exact=True).is_visible())

        clarifier.release.clear()
        page.get_by_role("button", name="重新生成问题").click()
        page.get_by_role("heading", name="模型正在生成澄清问题").wait_for()
        self.assertEqual(page.locator("fieldset.question-card").count(), 0)
        clarifier.release.set()
        page.get_by_role("heading", name="需求澄清", exact=True).wait_for()
        self.assertTrue(page.get_by_text("AI 生成问题", exact=True).is_visible())
        page.close()

        fallback_clarifier = ControlledClarifier(failures=1)
        self.service.clarifier = fallback_clarifier
        self.service.create("model-fallback")
        page, fallback_errors = self.new_page(375, 820)
        fallback_requests = []
        page.on("request", lambda request: fallback_requests.append(request) if request.url.endswith("/clarifications/fallback") else None)
        page.goto(self.base + "/tasks/model-fallback?stage=created")
        page.get_by_label("任务卡内容").fill("核心主题：新品发布")
        page.get_by_role("button", name="导入并冻结资料").click()
        page.get_by_role("heading", name="问题生成失败").wait_for()

        page.get_by_role("button", name="使用系统兜底问题").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("heading", name="确认使用系统兜底问题？").wait_for()
        dialog.get_by_role("button", name="取消").click()
        self.assertEqual(fallback_requests, [])
        self.assertEqual(page.locator("fieldset.question-card").count(), 0)

        page.get_by_role("button", name="使用系统兜底问题").click()
        page.get_by_role("dialog").get_by_role("button", name="确认使用兜底问题").click()
        page.get_by_role("heading", name="需求澄清", exact=True).wait_for()
        self.assertEqual(len(fallback_requests), 1)
        self.assertEqual(fallback_requests[0].post_data_json, {"confirm": True})
        self.assertTrue(page.get_by_text("系统补充问题", exact=True).is_visible())
        self.assert_no_page_overflow(page)
        self.assertEqual(errors, [])
        self.assertEqual(fallback_errors, [])
        page.close()


if __name__ == "__main__":
    unittest.main()
