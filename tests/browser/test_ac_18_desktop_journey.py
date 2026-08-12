"""AC-18: one real Chromium session completes the desktop journey."""
import json
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

    def api(self, method, path, body=None):
        result = self.page.evaluate(
            """async ({method, path, body}) => {
              const response = await fetch(path, {
                method,
                headers: {'Content-Type': 'application/json'},
                body: method === 'GET' ? undefined : JSON.stringify(body || {})
              });
              return {status: response.status, body: await response.json()};
            }""",
            {"method": method, "path": path, "body": body},
        )
        self.assertLess(result["status"], 400, json.dumps(result, ensure_ascii=False))
        return result["body"]

    def visit(self, path, heading):
        self.page.goto(self.base + path)
        self.page.get_by_role("heading", name=heading).wait_for()
        self.assertEqual(self.page.locator("body").evaluate("e => e.scrollWidth <= e.clientWidth"), True)

    def test_create_to_delivery_and_post_delivery_derivation(self):
        self.page.goto(self.base + "/healthz")
        self.api("POST", "/v1/tasks", {"task_id": "desktop", "mode": "manual"})
        self.api("POST", "/v1/tasks/desktop/input", {
            "source": {"goal": "发布方案", "audience": "客户", "topic": "增长", "页数": 3}
        })
        self.visit("/tasks/desktop", "任务/资料")
        self.assertIn("资料已可用于下一阶段", self.page.locator("body").inner_text())

        self.api("POST", "/v1/tasks/desktop/narrative/generate", {})
        self.api("POST", "/v1/tasks/desktop/narrative/confirm", {})
        self.api("POST", "/v1/tasks/desktop/outline/generate", {})
        self.visit("/tasks/desktop/outline", "大纲工作区")
        self.assertGreaterEqual(self.page.locator("textarea").count(), 2)
        self.api("POST", "/v1/tasks/desktop/outline/confirm", {})

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
        with self.page.expect_navigation():
            self.page.click("#run")
        self.assertIn("交付门禁：可交付", self.page.locator("body").inner_text())

        delivered = self.api("POST", "/v1/tasks/desktop/delivery/confirm", {
            "deck_hash": deck["hash"], "actor": "user"
        })
        self.assertEqual(delivered["state"]["status"], "completed")
        derived = self.api("POST", "/v1/tasks/desktop/delivery/derive", {
            "delivery_hash": delivered["delivery"]["hash"], "prompt": "统一使用蓝色主题"
        })
        self.assertNotEqual(derived["deck"]["hash"], deck["hash"])
        self.assertFalse(derived["state"]["delivery_confirmed"])
        summary = self.api("GET", "/v1/tasks/desktop/summary")
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["stage"], "deck")


if __name__ == "__main__":
    unittest.main()
