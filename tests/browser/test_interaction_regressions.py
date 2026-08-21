"""Interaction regressions from the real-API browser report.

P1-3: confirming a stage reached via a ?stage= deep link must advance to the
next stage instead of staying pinned on the confirmed page.
P1-4: a completed task renders the delivery stage as done (8/8), not 7/8.
P2-2: touch targets (brand home link, version timeline buttons) are >= 44px.
"""
import socket
import tempfile
import threading
import time
import unittest

import uvicorn

from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class PassingInspector:
    def inspect(self, outline, html): return {"passed": True, "issues": [], "model": "fixture"}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(sync_playwright, "playwright is required")
class InteractionRegressionGate(unittest.TestCase):
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
        self.app = create_app(self.service)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="critical"))
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

    def _complete_task(self, task_id):
        svc = self.service
        svc.create(task_id, "manual")
        svc.import_input(task_id, {"goal": "发布", "audience": "客户", "topic": "回归", "页数": 3})
        svc.generate_narrative(task_id)
        return svc

    def test_deep_link_confirm_advances_to_next_stage(self):
        svc = self._complete_task("deep-link")
        page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            # ?stage=narrative 深链进入：确认叙事结构后必须自动进入逐页大纲
            page.goto(f"{self.base}/tasks/deep-link?stage=narrative")
            page.get_by_role("heading", name="叙事结构", exact=True).wait_for()
            page.get_by_role("button", name="确认当前叙事结构").click()
            page.get_by_role("heading", name="逐页大纲", exact=True).wait_for(timeout=10000)
            self.assertIn("stage=outline", page.url)

            svc.generate_outline("deep-link")
            # ?stage=outline 深链进入：确认逐页大纲后必须自动进入样品
            page.goto(f"{self.base}/tasks/deep-link?stage=outline")
            page.get_by_role("heading", name="逐页大纲", exact=True).wait_for()
            page.get_by_role("button", name="确认当前逐页大纲").click()
            page.get_by_role("heading", name="样品", exact=True).wait_for(timeout=10000)
            self.assertIn("stage=sample", page.url)
        finally:
            page.close()

    def test_completed_task_shows_all_stages_finished(self):
        svc = self._complete_task("progress-8-8")
        svc.confirm_narrative("progress-8-8")
        svc.generate_outline("progress-8-8")
        svc.confirm_outline("progress-8-8")
        svc.generate_sample("progress-8-8")
        svc.confirm_sample("progress-8-8")
        svc.generate_deck("progress-8-8")
        svc.run_inspection("progress-8-8", 0)
        deck = svc.deck_view("progress-8-8")["deck"]
        svc.confirm_delivery("progress-8-8", deck["hash"])
        self.assertEqual(svc.get("progress-8-8")["status"], "completed")

        page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            page.goto(f"{self.base}/tasks/progress-8-8?stage=delivery")
            page.get_by_text("8 / 8 个阶段完成", exact=True).wait_for()
            self.assertEqual(page.locator('.progress-node[data-status="current"]').count(), 0)
            self.assertEqual(page.locator('.progress-node[href$="stage=delivery"]').get_attribute("data-status"), "completed")
        finally:
            page.close()

    def test_touch_targets_meet_44px_minimum(self):
        self._complete_task("touch-targets")
        page = self.browser.new_page(viewport={"width": 375, "height": 800})
        try:
            page.goto(f"{self.base}/tasks/touch-targets?stage=narrative")
            page.get_by_role("heading", name="叙事结构", exact=True).wait_for()
            brand = page.locator(".topbar__brand")
            box = brand.bounding_box()
            self.assertGreaterEqual(box["height"], 44, "品牌首页链接触控高度不足 44px")
            self.assertGreaterEqual(box["width"], 44, "品牌首页链接触控宽度不足 44px")
            # 移动端 .topbar__title 被隐藏后，链接仍必须有非空可访问名称。
            self.assertEqual(brand.get_attribute("aria-label"), "返回任务首页")
            self.assertEqual(page.get_by_role("link", name="返回任务首页").count(), 1, "品牌首页链接缺少可访问名称")
            preview = page.locator(".version-item .button", has_text="预览").first
            preview.wait_for()
            box = preview.bounding_box()
            self.assertGreaterEqual(box["height"], 44, "版本时间线按钮触控高度不足 44px")
        finally:
            page.close()

    def test_icon_buttons_keep_44px_width_in_flex_layout(self):
        self._complete_task("icon-width")
        page = self.browser.new_page(viewport={"width": 1024, "height": 800})
        try:
            page.goto(f"{self.base}/tasks/icon-width?stage=narrative")
            page.get_by_role("heading", name="叙事结构", exact=True).wait_for()
            buttons = page.locator(".topbar .icon-button:visible").all()
            self.assertTrue(buttons, "顶栏应至少有一个可见图标按钮")
            for button in buttons:
                box = button.bounding_box()
                self.assertGreaterEqual(box["width"], 44, "图标按钮触控宽度不足 44px")
                self.assertGreaterEqual(box["height"], 44, "图标按钮触控高度不足 44px")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
