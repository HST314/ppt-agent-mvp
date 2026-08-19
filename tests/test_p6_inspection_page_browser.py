"""P6 inspection interaction regression in the FastAPI single shell."""

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


class Inspector:
    def inspect(self, outline, html):
        return {"passed": False, "issues": [
            {"issue_id": "overflow-1", "severity": "blocker", "level": "element", "code": "overflow", "message": "元素溢出", "slide_id": "slide-1", "element_id": "title", "evidence": "越界", "suggestion": "缩小"},
            {"issue_id": "overflow-2", "severity": "warning", "level": "element", "code": "overflow", "message": "元素溢出", "slide_id": "slide-2", "element_id": "title", "evidence": "越界", "suggestion": "缩小"},
            {"issue_id": "density", "severity": "warning", "level": "slide", "code": "density", "message": "页面过密", "slide_id": "slide-2", "element_id": None, "evidence": "过密", "suggestion": "精简"},
        ]}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class InspectionPageBrowserTests(unittest.TestCase):
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
        self.svc = TaskService(WorkspaceStore(self.tmp.name), inspector=Inspector())
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
        self.posts = []
        self.page.on("request", lambda request: self.posts.append(request) if request.method == "POST" else None)
        self.page.goto(self.base + "/tasks/task?stage=review")
        self.page.get_by_role("heading", name="自检与修改", exact=True).wait_for()

    def tearDown(self):
        self.page.close()
        self.server.should_exit = True
        self.thread.join(5)
        self.tmp.cleanup()

    def test_locate_highlights_preview_and_batch_keeps_same_code(self):
        blocker = self.page.locator(".issue-card").filter(has_text="slide-1 / title")
        blocker.get_by_role("button", name="定位").click()
        frame = self.page.frame_locator("#deck-preview-frame")
        self.assertEqual(frame.locator('[data-element-id="title"]').first.get_attribute("data-inspection-highlight"), "true")
        self.assertIn("slide-1 / title", self.page.get_by_role("status").filter(has_text="已定位").inner_text())
        blocker.get_by_label("处置动作").select_option("defer")
        blocker.get_by_label("处置依据").fill("同类处理")
        with self.page.expect_request(lambda request: request.url.endswith("/issues/dispositions/batch")) as captured:
            blocker.get_by_role("button", name="处置同 code 问题").click()
        body = json.loads(captured.value.post_data)
        self.assertEqual(body["issue_ids"], ["overflow-1"])


if __name__ == "__main__":
    unittest.main()
