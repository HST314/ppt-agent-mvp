"""FastAPI application shell browser, responsive and accessibility gate."""
import socket
import tempfile
import threading
import time
import unittest

import uvicorn
from playwright.sync_api import sync_playwright

from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app


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


if __name__ == "__main__":
    unittest.main()
