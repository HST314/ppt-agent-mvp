"""AC-18 historical desktop journey, migrated to the FastAPI single shell."""

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


class PassingInspector:
    def inspect(self, outline, html):
        return {"passed": True, "issues": [], "model": "ac18-fixed-fixture"}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DesktopJourney(unittest.TestCase):
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
        self.service = TaskService(WorkspaceStore(self.tmp.name), inspector=PassingInspector())
        port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.service), host="127.0.0.1", port=port, log_level="critical"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started)
        self.base = f"http://127.0.0.1:{port}"
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.errors = []
        self.page.on("console", lambda message: self.errors.append(message.text) if message.type == "error" else None)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.page.close()
        self.server.should_exit = True
        self.thread.join(5)
        self.tmp.cleanup()

    def test_create_to_delivery_and_post_delivery_derivation(self):
        page = self.page
        page.goto(self.base + "/")
        page.get_by_label("任务 ID").fill("desktop")
        page.get_by_role("button", name="创建任务并进入工作台").click()
        page.get_by_role("heading", name="任务/资料", exact=True).wait_for()
        page.get_by_label("任务卡格式").select_option("json")
        page.get_by_label("任务卡内容").fill('{"goal":"发布方案","audience":"客户","topic":"增长","页数":3}')
        page.get_by_role("button", name="导入并冻结资料").click()
        page.get_by_role("heading", name="澄清", exact=True).wait_for()

        page.get_by_role("button", name="生成叙事结构", exact=True).click()
        page.get_by_role("heading", name="叙事结构", exact=True).wait_for()
        page.get_by_role("button", name="确认当前叙事结构").click()
        page.get_by_role("heading", name="逐页大纲", exact=True).wait_for()
        page.get_by_role("button", name="生成逐页大纲", exact=True).click()
        page.get_by_role("button", name="确认当前逐页大纲").wait_for()
        page.get_by_role("button", name="确认当前逐页大纲").click()

        page.get_by_role("heading", name="样品", exact=True).wait_for()
        page.get_by_role("button", name="生成 HTML 样品").click()
        page.get_by_role("button", name="确认当前样品并进入全稿").wait_for()
        page.get_by_role("button", name="确认当前样品并进入全稿").click()
        page.get_by_role("button", name="生成完整演示稿").click()
        page.get_by_role("heading", name="检查", exact=True).wait_for()
        page.get_by_text("可进入交付", exact=True).wait_for()
        page.get_by_role("link", name="前往交付").click()

        page.get_by_role("heading", name="交付", exact=True).wait_for()
        page.get_by_role("button", name="确认最终交付").click()
        page.get_by_role("dialog").get_by_role("button", name="确认并生成离线交付").click()
        page.get_by_text("已完成", exact=True).first.wait_for()
        deck = self.service.deck_view("desktop")["deck"]
        page.get_by_label("派生要求").fill("统一使用蓝色主题")
        page.get_by_role("button", name="从该交付派生新候选").click()
        page.get_by_role("heading", name="全稿", exact=True).wait_for()

        derived = self.service.deck_view("desktop")["deck"]
        self.assertNotEqual(derived["hash"], deck["hash"])
        self.assertEqual(self.service.status_summary("desktop")["stage"], "deck")
        self.assertTrue(page.locator("body").evaluate("node => node.scrollWidth <= node.clientWidth"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
