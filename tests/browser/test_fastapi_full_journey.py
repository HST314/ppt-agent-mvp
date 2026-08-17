"""Unified FastAPI shell journey for every migrated stage."""
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


class FastAPIFullJourneyGate(unittest.TestCase):
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
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.service), host="127.0.0.1", port=port, log_level="critical"))
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

    def test_create_to_delivery_and_derive_in_one_shell(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 950})
        errors = []
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(self.base + "/")
        page.get_by_label("任务 ID").fill("step2-journey")
        page.get_by_role("button", name="创建任务并进入工作台").click()
        page.get_by_role("heading", name="任务/资料", exact=True).wait_for()

        page.get_by_label("任务卡格式").select_option("markdown")
        page.get_by_label("任务卡内容").fill("演示目标：新品发布\n受众：管理层\n核心主题：年度增长")
        page.get_by_role("button", name="导入并冻结资料").click()
        page.get_by_role("heading", name="澄清", exact=True).wait_for()
        self.assertFalse(page.get_by_role("dialog", name="没有检测到图片资源").is_visible())
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
        page.get_by_role("button", name="生成完整演示稿").wait_for()
        page.get_by_role("button", name="生成完整演示稿").click()

        page.get_by_role("link", name="前往独立检查", exact=True).wait_for()
        self.assertEqual(page.locator('.stage-link[href$="stage=review"]').get_attribute("data-status"), "available")
        page.get_by_role("link", name="前往独立检查", exact=True).click()
        page.get_by_role("heading", name="检查", exact=True).wait_for()
        page.get_by_text("尚未检查", exact=True).wait_for()
        page.get_by_role("button", name="执行独立检查", exact=True).click()
        page.get_by_role("button", name="保存处置").wait_for()
        page.get_by_label("处置动作").select_option("waive")
        page.get_by_label("处置依据").fill("已人工核对，接受当前版式")
        page.get_by_role("button", name="保存处置").click()
        page.get_by_text("可进入交付", exact=True).wait_for()
        page.get_by_role("link", name="前往交付").click()
        page.get_by_role("heading", name="交付", exact=True).wait_for()
        page.get_by_role("button", name="确认最终交付").click()
        page.get_by_role("dialog").get_by_role("button", name="确认并生成离线交付").click()
        page.get_by_text("已完成", exact=True).first.wait_for()

        for width, stage, label in ((375, "narrative", "叙事结构"), (768, "sample", "样品"), (1024, "review", "检查"), (1440, "delivery", "交付")):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{self.base}/tasks/step2-journey?stage={stage}")
            page.get_by_role("heading", name=label, exact=True).wait_for()
            page.locator("#stage-content .card").first.wait_for()
            self.assertLessEqual(page.locator("body").evaluate("node => node.scrollWidth"), page.locator("body").evaluate("node => node.clientWidth"))
            mutating = page.locator('#stage-content [data-mutates="true"]')
            if mutating.count():
                self.assertTrue(mutating.first.is_disabled())

        page.goto(self.base + "/tasks/step2-journey?stage=delivery")
        page.get_by_label("派生要求").fill("增加一页风险摘要")
        page.get_by_role("button", name="从该交付派生新候选").click()
        page.get_by_role("heading", name="全稿", exact=True).wait_for()

        self.assertLessEqual(page.locator("body").evaluate("node => node.scrollWidth"), page.locator("body").evaluate("node => node.clientWidth"))
        self.assertEqual(errors, [])
        page.close()


if __name__ == "__main__":
    unittest.main()
