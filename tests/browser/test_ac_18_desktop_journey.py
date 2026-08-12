"""AC-18: one real Chromium session completes the desktop journey."""
import tempfile
import threading
import unittest
from wsgiref.simple_server import WSGIRequestHandler, make_server

from playwright.sync_api import sync_playwright

from ppt_agent.api import App
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class PassingInspector:
    def inspect(self, outline, html):
        return {"passed": True, "issues": [], "model": "ac18-fixed-fixture"}


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class DesktopJourney(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        # Browser absence is a gate failure, never a skipped test.
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(self.tmp.name)
        self.service = TaskService(self.store, inspector=PassingInspector())
        self.server = make_server("127.0.0.1", 0, App(self.service), handler_class=QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})

    def tearDown(self):
        self.page.close()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp.cleanup()

    def visit(self, path, heading):
        self.page.goto(self.base + path)
        self.page.get_by_role("heading", name=heading).wait_for()
        self.assertEqual(self.page.locator("body").evaluate("e => e.scrollWidth <= e.clientWidth"), True)

    def test_create_to_delivery_and_post_delivery_derivation(self):
        self.visit("/", "PPT Agent 桌面工作区")
        self.page.get_by_label("任务 ID").fill("desktop")
        with self.page.expect_navigation():
            self.page.get_by_role("button", name="创建任务").click()

        self.page.get_by_label("格式").select_option("json")
        self.page.get_by_role("textbox", name="任务卡").fill(
            '{"goal":"发布方案","audience":"客户","topic":"增长","页数":3}'
        )
        with self.page.expect_navigation():
            self.page.get_by_role("button", name="导入并扫描授权资源").click()
        self.assertIn("资料已可用于下一阶段", self.page.locator("body").inner_text())

        self.visit("/tasks/desktop/outline", "大纲工作区")
        narrative = self.page.get_by_role("region", name="叙事结构")
        with self.page.expect_navigation():
            narrative.get_by_role("button", name="生成/整体重生成").click()
        narrative = self.page.get_by_role("region", name="叙事结构")
        with self.page.expect_navigation():
            narrative.get_by_role("button", name="确认叙事").click()
        outline = self.page.get_by_role("region", name="逐页大纲")
        with self.page.expect_navigation():
            outline.get_by_role("button", name="生成/整体重生成").click()
        outline = self.page.get_by_role("region", name="逐页大纲")
        with self.page.expect_navigation():
            outline.get_by_role("button", name="确认大纲").click()

        self.visit("/tasks/desktop/samples", "HTML 样品页")
        with self.page.expect_navigation():
            self.page.click("#generate")
        self.assertEqual(len(self.service.sample_view("desktop")["selection"]["slide_ids"]), 2)
        with self.page.expect_navigation():
            self.page.click("#confirm")

        self.visit("/tasks/desktop/deck", "完整 HTML 演示稿")
        with self.page.expect_navigation():
            self.page.click("#generate")
        deck = self.service.deck_view("desktop")["deck"]
        self.assertEqual(len(deck["metadata"]["page_hashes"]), 3)
        self.assertEqual(self.page.frame_locator("#previewFrame").locator("[data-slide-id]").count(), 3)

        self.visit("/tasks/desktop/inspection", "独立检查与人工审核")
        # Deck generation already triggers independent inspection; the visible
        # control reruns it so this gate proves the review action is operable.
        with self.page.expect_navigation(): self.page.click("#run")
        self.assertIn("交付门禁：可交付", self.page.locator("body").inner_text())

        self.visit("/tasks/desktop/delivery", "交付与派生")
        with self.page.expect_navigation():
            self.page.get_by_role("button", name="确认最终交付").click()
        self.assertIn("交付状态：已确认", self.page.locator("body").inner_text())
        self.page.get_by_label("派生要求").fill("统一使用蓝色主题")
        with self.page.expect_navigation():
            self.page.get_by_role("button", name="从已交付版本派生").click()

        derived = self.service.deck_view("desktop")["deck"]
        self.assertNotEqual(derived["hash"], deck["hash"])
        summary = self.service.status_summary("desktop")
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["stage"], "deck")


if __name__ == "__main__":
    unittest.main()
