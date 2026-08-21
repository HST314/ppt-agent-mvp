"""定稿门禁的浏览器回归：阻断未清零时默认定稿被禁，带风险定稿显式留痕。"""

import json
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


class BlockerInspector:
    def inspect(self, outline, html):
        return {"passed": False, "issues": [
            {"issue_id": "overflow-1", "severity": "blocker", "level": "element", "code": "overflow", "message": "元素溢出", "slide_id": "slide-1", "element_id": "title", "evidence": "越界", "suggestion": "缩小"},
        ]}


class PassingInspector:
    def inspect(self, outline, html):
        return {"passed": True, "issues": [], "model": "fixture"}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FinalizeGateBrowserBase(unittest.TestCase):
    inspector = None

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
        self.svc = TaskService(WorkspaceStore(self.tmp.name), inspector=self.inspector)
        self.svc.create("task")
        self.svc.import_input("task", {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 3})
        self.svc.generate_narrative("task"); self.svc.confirm_narrative("task")
        self.svc.generate_outline("task"); self.svc.confirm_outline("task")
        self.svc.generate_sample("task"); self.svc.confirm_sample("task")
        self.svc.generate_deck("task"); self.svc.run_inspection("task", 0)
        port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.svc), host="127.0.0.1", port=port, log_level="critical"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started)
        self.base = f"http://127.0.0.1:{port}"
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.page.goto(self.base + "/tasks/task?stage=review")
        self.page.get_by_role("heading", name="自检与修改", exact=True).wait_for()
        # 自检页正文为异步 hydrate（skeleton 先挂载，数据返回后整体替换为 .stage-grid），
        # 仅等标题会在定稿按钮尚未渲染时产生竞态。
        self.page.locator("#stage-content .stage-grid").wait_for()

    def tearDown(self):
        self.page.close()
        self.server.should_exit = True
        self.thread.join(5)
        self.tmp.cleanup()


class FinalizeBlockedBrowserTests(FinalizeGateBrowserBase):
    inspector = BlockerInspector()

    def test_default_finalize_is_disabled_and_risk_path_requires_rationale(self):
        finalize = self.page.get_by_role("button", name="确定终稿", exact=True)
        self.assertTrue(finalize.is_disabled())
        self.assertIn("默认定稿已禁止", finalize.get_attribute("aria-description"))
        notice = self.page.locator(".finalize-actions .notice--danger")
        self.assertIn("仍有 1 项未处置阻断问题", notice.inner_text())

        risk = self.page.get_by_role("button", name="带风险定稿", exact=True)
        self.assertTrue(risk.is_disabled())
        self.assertEqual(risk.get_attribute("aria-description"), "请先填写风险依据")
        self.page.locator("#risk-finalize-rationale").fill("客户已确认接受越界风险")
        self.assertFalse(risk.is_disabled())

    def test_risk_finalize_posts_explicit_flag_and_labels_delivery_page(self):
        self.page.locator("#risk-finalize-rationale").fill("客户已确认接受越界风险")
        self.page.get_by_role("button", name="带风险定稿", exact=True).click()
        dialog = self.page.get_by_role("dialog")
        self.assertIn("带风险定稿", dialog.get_by_role("heading").inner_text())
        with self.page.expect_request(lambda request: request.url.endswith("/deck/finalize")) as captured:
            dialog.get_by_role("button", name="我已了解风险，确认带风险定稿").click()
        body = json.loads(captured.value.post_data)
        self.assertTrue(body["allow_risk"])
        self.assertEqual(body["risk_rationale"], "客户已确认接受越界风险")

        self.page.get_by_role("heading", name="交付", exact=True).wait_for()
        # 交付页正文为异步 hydrate：等待可识别的异步内容完成态（带风险终稿提示挂载即
        # 说明 delivery stage 的三个接口已返回、.stage-grid 已整体替换 skeleton），再断言。
        risk_notice = self.page.get_by_text("该版本为带风险终稿", exact=True)
        risk_notice.wait_for()
        self.assertTrue(risk_notice.is_visible())
        self.assertGreaterEqual(self.page.get_by_text("带风险终稿", exact=True).count(), 1)
        self.assertIn("客户已确认接受越界风险", self.page.locator(".stage-grid").inner_text())
        # 带风险终稿不放松发布门禁：渲染预检仍未通过，发布保持禁止。
        publish_blocked = self.page.get_by_text("渲染预检未通过，已禁止发布", exact=True)
        publish_blocked.wait_for()
        self.assertTrue(publish_blocked.is_visible())


class FinalizeStandardBrowserTests(FinalizeGateBrowserBase):
    inspector = PassingInspector()

    def test_finalize_without_blockers_sends_standard_request(self):
        finalize = self.page.get_by_role("button", name="确定终稿", exact=True)
        self.assertFalse(finalize.is_disabled())
        self.assertEqual(self.page.locator("#risk-finalize-rationale").count(), 0)
        finalize.click()
        dialog = self.page.get_by_role("dialog")
        with self.page.expect_request(lambda request: request.url.endswith("/deck/finalize")) as captured:
            dialog.get_by_role("button", name="确定终稿并前往交付").click()
        body = json.loads(captured.value.post_data)
        self.assertNotIn("allow_risk", body)
        self.assertNotIn("risk_rationale", body)

        self.page.get_by_role("heading", name="交付", exact=True).wait_for()
        # 正常定稿无专属提示文本，等待 hydration 完成态（.stage-grid 替换 skeleton）后再做零计数断言。
        self.page.locator("#stage-content .stage-grid").wait_for()
        self.assertEqual(self.page.get_by_text("带风险终稿", exact=True).count(), 0)


if __name__ == "__main__":
    unittest.main()
