"""P4 sample interaction regression in the FastAPI single shell."""

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


class SamplePageBrowserTests(unittest.TestCase):
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
        self.svc = TaskService(WorkspaceStore(self.tmp.name))
        self.svc.create("task")
        self.svc.import_input("task", {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 3})
        self.svc.generate_narrative("task")
        self.svc.confirm_narrative("task")
        self.svc.generate_outline("task")
        self.svc.confirm_outline("task")
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

    def tearDown(self):
        self.page.close()
        self.server.should_exit = True
        self.thread.join(5)
        self.tmp.cleanup()

    def wait_for_versions(self, count, timeout=15):
        deadline = time.monotonic() + timeout
        view = self.svc.sample_view("task")
        while time.monotonic() < deadline and (
            len(view["versions"]) < count
            or not view["sample"]
            or view["sample"]["version"] < count
        ):
            time.sleep(0.05)
            view = self.svc.sample_view("task")
        self.assertGreaterEqual(len(view["versions"]), count)
        self.assertIsNotNone(view["sample"])
        self.assertGreaterEqual(view["sample"]["version"], count)
        return view

    def test_generate_modify_preview_and_confirm_in_single_shell(self):
        page = self.page
        page.goto(self.base + "/tasks/task?stage=sample")
        page.get_by_role("heading", name="样品", exact=True).wait_for()
        page.get_by_label("视觉要求").fill("首屏主视觉")
        page.get_by_role("button", name="生成 HTML 样品").click()
        first = self.wait_for_versions(1)
        page.reload()
        page.get_by_role("button", name="确认当前样品并进入全稿").wait_for()
        slide_id = first["selection"]["slide_ids"][0]
        self.assertTrue(page.locator("#sample-preview").get_attribute("src").endswith(first["sample"]["hash"]))

        page.get_by_label("视觉要求").fill("当前页标题更醒目")
        page.get_by_label("页面 ID", exact=True).fill(slide_id)
        page.get_by_role("button", name="提交样品修改").click()
        modified = self.wait_for_versions(2)
        page.reload()
        page.get_by_role("button", name="确认当前样品并进入全稿").wait_for()
        understanding = modified["sample"]["metadata"]["scope_understanding"]
        self.assertEqual(understanding["scope"], "page")
        self.assertEqual(understanding["basis"], "prompt_semantics")

        page.get_by_role("button", name="确认当前样品并进入全稿").click()
        page.get_by_role("heading", name="全稿", exact=True).wait_for()
        self.assertTrue(self.svc.sample_view("task")["confirmation"])


if __name__ == "__main__":
    unittest.main()
