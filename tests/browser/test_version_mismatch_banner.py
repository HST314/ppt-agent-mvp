"""前后端版本不一致时，前端必须展示 backend_commit 并明确提示重启。"""

import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import uvicorn

from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app
import ppt_agent.web.app as web_app
from ppt_agent.web.assets import FRONTEND_BUILD

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

STALE_BUILD = "2000.01.01.000000000000"
STALE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(sync_playwright, "playwright is required")
class VersionMismatchBannerTests(unittest.TestCase):
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
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})

    def tearDown(self):
        self.page.close()
        self.tmp.cleanup()

    def serve(self, *, stale):
        if stale:
            for patch in (
                mock.patch.object(web_app, "FRONTEND_BUILD", STALE_BUILD),
                mock.patch.object(web_app, "backend_commit", lambda: STALE_COMMIT),
            ):
                patch.start()
                self.addCleanup(patch.stop)
        port = free_port()
        self.app = create_app(self.svc)
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="critical"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.started)
        self.addCleanup(self.stop_server, server, thread)
        return f"http://127.0.0.1:{port}"

    @staticmethod
    def stop_server(server, thread):
        server.should_exit = True
        thread.join(5)

    def open_settings_dialog(self):
        self.page.get_by_role("button", name="打开设置").click()
        dialog = self.page.locator("dialog[open]")
        dialog.wait_for()
        return dialog

    def test_stale_backend_shows_restart_banner_and_commit(self):
        base = self.serve(stale=True)
        self.page.goto(base + "/")
        banner = self.page.locator("[data-version-mismatch-banner]")
        banner.wait_for(timeout=15000)
        self.assertIn("前端与后端版本不一致，请重启后端服务", banner.text_content())
        self.assertIn(STALE_BUILD, banner.text_content())
        self.assertIn(STALE_COMMIT[:12], banner.text_content())
        self.assertIn("python -m uvicorn main_front:app", banner.text_content())
        self.assertEqual(banner.get_attribute("role"), "alert")
        self.assertTrue(self.page.get_by_text("版本不一致·需重启", exact=True).first.is_visible())
        dialog = self.open_settings_dialog()
        details = dialog.locator("[data-runtime-version-details]")
        self.assertIn(FRONTEND_BUILD, details.text_content())
        self.assertIn(STALE_BUILD, details.text_content())
        self.assertIn(STALE_COMMIT, details.text_content())
        self.assertIn("请重启后端服务", dialog.locator(".version-warning").text_content())

    def test_matching_versions_show_no_banner_and_consistent_status(self):
        base = self.serve(stale=False)
        self.page.goto(base + "/")
        self.page.locator(".badge--connection", has_text="后端可达").wait_for(timeout=15000)
        self.assertEqual(self.page.locator("[data-version-mismatch-banner]").count(), 0)
        self.assertEqual(self.page.get_by_text("版本不一致·需重启", exact=True).count(), 0)
        dialog = self.open_settings_dialog()
        details = dialog.locator("[data-runtime-version-details]")
        self.assertIn(FRONTEND_BUILD, details.text_content())
        self.assertIn("前后端版本一致", details.text_content())

    def track_posts(self):
        posts = []
        self.page.on("request", lambda request: posts.append(request) if request.method == "POST" else None)
        return posts

    def force_click(self, locator):
        # 单 JS 任务内完成启用与点击，避免 15 秒运行态轮询重新禁用造成竞态；
        # 用于验证“即使按钮态被绕过，派发前二次校验仍然拦截”。
        locator.evaluate("node => { node.disabled = false; node.click(); }")

    def assert_blocked_dispatch(self, posts, fragment):
        message = self.page.locator(".stage-message", has_text="版本不一致")
        message.wait_for(timeout=10000)
        self.assertIn("重启", message.text_content())
        self.assertFalse(any(fragment in request.url for request in posts), f"不应发出 POST {fragment}")
        self.assertEqual(self.app.state.job_service.list("task"), [])

    def test_mismatch_blocks_sample_confirm_dispatch(self):
        self.svc.create("task")
        self.svc.import_input("task", {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 3})
        self.svc.generate_narrative("task")
        self.svc.confirm_narrative("task")
        self.svc.generate_outline("task")
        self.svc.confirm_outline("task")
        self.svc.generate_sample("task")
        base = self.serve(stale=True)
        posts = self.track_posts()
        self.page.goto(base + "/tasks/task?stage=sample")
        self.page.locator("[data-version-mismatch-banner]").wait_for(timeout=15000)
        confirm = self.page.get_by_role("button", name="确认当前样品并进入全稿")
        confirm.wait_for()
        self.assertTrue(confirm.is_disabled())
        self.assertIn("重启", confirm.get_attribute("title") or "")
        self.force_click(confirm)
        self.assert_blocked_dispatch(posts, "/samples/confirm")
        self.assertFalse(self.svc.sample_view("task")["confirmation"])
        self.assertEqual(self.svc.get("task")["stage"], "sample")

    def test_mismatch_blocks_import_dispatch(self):
        self.svc.create("task")
        base = self.serve(stale=True)
        posts = self.track_posts()
        self.page.goto(base + "/tasks/task")
        self.page.locator("[data-version-mismatch-banner]").wait_for(timeout=15000)
        self.page.locator("#task-card-source").fill("演示目标：发布\n受众：客户\n核心主题：方案")
        submit = self.page.get_by_role("button", name="导入并冻结资料")
        submit.wait_for()
        self.assertTrue(submit.is_disabled())
        self.assertIn("重启", submit.get_attribute("title") or "")
        self.force_click(submit)
        self.assert_blocked_dispatch(posts, "/tasks/task/input")
        self.assertIsNone(self.svc.input_view("task")["snapshot"])
        self.assertEqual(self.svc.get("task")["stage"], "created")

    def test_mismatch_blocks_answers_dispatch(self):
        self.svc.create("task")
        self.svc.import_input("task", {"goal": "发布新品"})
        base = self.serve(stale=True)
        posts = self.track_posts()
        self.page.goto(base + "/tasks/task?stage=clarification")
        self.page.locator("[data-version-mismatch-banner]").wait_for(timeout=15000)
        self.page.locator(".question-card").first.wait_for()
        others = self.page.locator(".question-other__input")
        for index in range(others.count()):
            others.nth(index).fill("测试答案")
        submit = self.page.get_by_role("button", name="提交答案并继续")
        submit.wait_for()
        self.assertTrue(submit.is_disabled())
        self.assertIn("重启", submit.get_attribute("title") or "")
        self.force_click(submit)
        self.assert_blocked_dispatch(posts, "/clarifications/answers")
        self.assertEqual(self.svc.input_view("task")["clarification"]["answers"], {})
        self.assertEqual(self.svc.get("task")["stage"], "clarification")
