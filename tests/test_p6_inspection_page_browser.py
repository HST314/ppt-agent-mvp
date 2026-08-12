"""P6 inspection-page interaction regression in a real Chromium browser."""
import json
import tempfile
import threading
import unittest
from wsgiref.simple_server import WSGIRequestHandler, make_server

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from ppt_agent.api import App
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class Inspector:
    def inspect(self, outline, html):
        return {"passed": False, "issues": [
            {"issue_id": "overflow-1", "severity": "blocker", "level": "element", "code": "overflow", "message": "溢出", "slide_id": "slide-1", "element_id": "title", "evidence": "越界", "suggestion": "缩小"},
            {"issue_id": "overflow-2", "severity": "warning", "level": "element", "code": "overflow", "message": "溢出", "slide_id": "slide-2", "element_id": "title", "evidence": "越界", "suggestion": "缩小"},
            {"issue_id": "density", "severity": "warning", "level": "slide", "code": "density", "message": "过密", "slide_id": "slide-2", "element_id": None, "evidence": "过密", "suggestion": "精简"},
        ]}


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class InspectionPageBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("playwright 未安装")
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:
            cls.pw.stop()
            raise unittest.SkipTest(f"chromium 不可用：{exc}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None): cls.browser.close()
        if getattr(cls, "pw", None): cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = TaskService(WorkspaceStore(self.tmp.name), inspector=Inspector())
        self.svc.create("task")
        self.svc.import_input("task", {"goal":"发布", "audience":"客户", "topic":"方案", "页数":3})
        self.svc.generate_narrative("task"); self.svc.confirm_narrative("task")
        self.svc.generate_outline("task"); self.svc.confirm_outline("task")
        self.svc.generate_sample("task"); self.svc.confirm_sample("task")
        self.svc.generate_deck("task"); self.svc.run_inspection("task", 0)
        self.server = make_server("127.0.0.1", 0, App(self.svc), handler_class=QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.page = self.__class__.browser.new_page()
        self.posts = []
        self.page.on("request", lambda request: self.posts.append(request) if request.method == "POST" else None)
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/tasks/task/inspection")

    def tearDown(self):
        self.page.close(); self.server.shutdown(); self.thread.join(); self.server.server_close(); self.tmp.cleanup()

    def test_locate_highlights_iframe_target_and_batch_keeps_same_code(self):
        self.page.click('li[data-issue="overflow-1"] .locate')
        frame = self.page.frame_locator("#preview")
        self.assertEqual(frame.locator('[data-element-id="title"]').first.get_attribute("data-inspection-highlight"), "true")
        self.assertIn("slide-1 / title", self.page.text_content("#location"))
        self.page.fill('li[data-issue="overflow-1"] .rationale', "同类处理")
        self.page.click('li[data-issue="overflow-1"] .dispose-batch')
        self.page.wait_for_timeout(100)
        requests = [r for r in self.posts if r.url.endswith("/issues/dispositions/batch")]
        body = json.loads(requests[-1].post_data)
        self.assertEqual(body["issue_ids"], ["overflow-1", "overflow-2"])


if __name__ == "__main__":
    unittest.main()
