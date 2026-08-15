"""FastAPI application shell browser, responsive and accessibility gate."""
import socket
import tempfile
import threading
import time
import unittest

import uvicorn
from playwright.sync_api import sync_playwright

from ppt_agent.errors import GatewayError
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app


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
        config = uvicorn.Config(create_app(self.service), host="127.0.0.1", port=port, log_level="critical")
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
        self.assertEqual(page.locator(".stage-list > li").count(), 8)
        self.assertEqual(page.locator('[aria-current="step"]').inner_text().splitlines()[0], "1")
        self.assertIn("前置条件", page.locator('.stage-link[aria-disabled="true"]').first.get_attribute("title"))

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
                page.get_by_role("button", name="打开任务与阶段导航").click()
                self.assertEqual(page.locator(".sidebar").get_attribute("data-open"), "true")
                self.assertEqual(page.locator(".stage-list > li").count(), 8)
                page.keyboard.press("Escape")
                self.assertEqual(page.locator(".sidebar").get_attribute("data-open"), "false")
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

        page.get_by_role("heading", name="模型正在阅读任务卡").wait_for()
        self.assertTrue(clarifier.started.wait(2))
        self.assertEqual(page.locator("fieldset.question-card").count(), 0)
        self.assertEqual(page.get_by_text("无需额外澄清", exact=True).count(), 0)
        self.assertTrue(page.get_by_text("模型完成前不会展示问题，也不会自动切换到系统兜底题。", exact=True).is_visible())
        self.assertEqual(page.get_by_role("button", name="取消后台任务").count(), 0)
        self.assert_no_page_overflow(page)

        clarifier.release.set()
        page.get_by_role("heading", name="需求澄清", exact=True).wait_for()
        self.assertEqual(page.locator("fieldset.question-card").count(), 1)
        self.assertTrue(page.get_by_text("AI 生成问题", exact=True).is_visible())
        self.assertTrue(page.get_by_text("本次发布需要管理层批准预算，还是仅同步项目进展？", exact=True).is_visible())
        self.assertEqual(errors, [])
        page.close()

    def test_unready_model_is_not_shown_as_green_and_fallback_remains_available(self):
        class UnreadyClarifier:
            model = "unready-browser-model"
            calls = 0
            def probe_capabilities(self):
                raise GatewayError(
                    "模型服务认证失败，请联系管理员检查凭据",
                    code="model_authentication_failed",
                )
            def clarify(self, _payload):
                self.calls += 1
                raise AssertionError("unready model must not be called")

        clarifier = UnreadyClarifier()
        self.service.clarifier = clarifier
        self.service.initialize_runtime()
        self.service.create("runtime-unready")
        page, errors = self.new_page(375, 820, "reduce")
        page.goto(self.base + "/tasks/runtime-unready?stage=created")
        page.get_by_role("button", name="打开设置").click()
        settings = page.get_by_role("dialog")
        settings.get_by_text("浏览器在线", exact=True).wait_for()
        settings.get_by_text("后端可达", exact=True).wait_for()
        settings.get_by_text("模型不可用", exact=True).wait_for()
        self.assertEqual(settings.get_by_text("模型可用", exact=True).count(), 0)
        self.assertTrue(settings.get_by_text("失败检查", exact=True).is_visible())
        self.assertTrue(settings.get_by_text("能力契约", exact=True).is_visible())
        self.assertTrue(settings.get_by_text("探测 ID", exact=True).is_visible())
        self.assertRegex(settings.locator("dl.metadata-list").inner_text(),r"runtime-probe-[0-9a-f]{32}")
        settings.get_by_role("button", name="关闭").click()

        page.get_by_label("任务卡内容").fill("核心主题：新品发布")
        page.get_by_role("button", name="导入并冻结资料").click()
        page.get_by_role("heading", name="问题生成失败").wait_for()
        retry = page.get_by_role("button", name="重新生成问题")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not retry.is_disabled():
            time.sleep(0.01)
        self.assertTrue(retry.is_disabled())
        self.assertFalse(page.get_by_role("button", name="使用系统兜底问题").is_disabled())
        self.assertTrue(page.get_by_text("model_authentication_failed", exact=True).is_visible())
        self.assertTrue(page.get_by_text("能力契约", exact=True).is_visible())
        self.assertTrue(page.get_by_role("button", name="复制探测 ID").is_visible())
        self.assertTrue(page.get_by_text("这是确定性配置故障", exact=False).is_visible())

        page.get_by_role("button", name="使用系统兜底问题").click()
        page.get_by_role("dialog").get_by_role("button", name="确认使用兜底问题").click()
        page.get_by_role("heading", name="需求澄清", exact=True).wait_for()
        self.assertEqual(clarifier.calls, 0)
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
        page.get_by_role("heading", name="模型正在阅读任务卡").wait_for()
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
