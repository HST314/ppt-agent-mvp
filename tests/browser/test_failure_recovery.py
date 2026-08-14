"""Browser regression for the clarification failure -> gated rebuild recovery chain."""
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

# Two free-text inputs that normalize to the same task card (no parseable
# goal/audience/topic lines), so only the raw source hash distinguishes them.
SOURCE_A = "天赐湾镇2026下半年重点项目与工作计划整体安排"
SOURCE_B = "天赐湾镇文化站墙壁翻新与宣传内容更新方案"


def model_question():
    return {
        "question_id": "business-decision",
        "field_path": "decision",
        "prompt": "本次发布需要管理层批准预算，还是仅同步项目进展？",
        "helper_text": "这会决定论证结构和数据深度。",
        "options": [
            {"value": "approve", "label": "批准预算", "description": "以决策材料为主"},
            {"value": "update", "label": "同步进展", "description": "以项目状态为主"},
        ],
        "allow_other": True,
        "blocking": True,
    }


class FlakyClarifier:
    model = "browser-clarifier"

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def clarify(self, _payload):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("model gateway unavailable")
        return {"questions": [model_question()], "model": self.model}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FailureRecoveryBrowserGate(unittest.TestCase):
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
        self.clarifier = FlakyClarifier(failures=1)
        self.service = TaskService(WorkspaceStore(self.tmp.name), clarifier=self.clarifier)
        self.app = create_app(self.service)
        port = free_port()
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

    def dismiss_resource_reminder(self, page):
        reminder = page.get_by_role("heading", name="没有检测到图片资源", exact=True)
        try:
            reminder.wait_for(timeout=2000)
        except Exception:
            return
        # Escape dismisses without navigating; the primary action would jump
        # back to the clarification stage for a task stuck there.
        page.keyboard.press("Escape")
        self.assertFalse(reminder.is_visible())

    def test_failed_clarification_recovers_through_backfilled_gated_rebuild(self):
        self.service.create("recover")
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        import_posts = []
        page.on("request", lambda request: import_posts.append(request) if request.method == "POST" and request.url.endswith("/input") else None)

        # 1. Import source A; the single model failure closes the flow.
        page.goto(self.base + "/tasks/recover?stage=created")
        page.get_by_label("任务卡内容").fill(SOURCE_A)
        page.get_by_role("button", name="导入并冻结资料").click()
        page.get_by_role("heading", name="问题生成失败").wait_for()
        self.assertEqual(page.locator("fieldset.question-card").count(), 0)
        first_snapshot = self.service.input_view("recover")["snapshot_hash"]
        self.assertEqual(self.clarifier.calls, 1)

        # 2. Back to input stage: the frozen source is backfilled and the
        # submit action stays disabled while nothing changed.
        page.locator(".stage-list").get_by_role("link", name="任务/资料").click()
        page.get_by_role("heading", name="更新任务资料").wait_for()
        self.dismiss_resource_reminder(page)
        textarea = page.get_by_label("任务卡内容")
        self.assertEqual(textarea.input_value(), SOURCE_A)
        self.assertEqual(page.get_by_label("任务卡格式").input_value(), "markdown")
        submit = page.get_by_role("button", name="重建资料快照")
        self.assertTrue(submit.is_disabled())
        self.assertTrue(page.get_by_text("资料未变化，无需重建；修改原文后才可提交。", exact=True).is_visible())

        # 3. Edit A -> B (same normalized card): still gated until the
        # explicit rebuild checkbox is checked.
        textarea.fill(SOURCE_B)
        self.assertTrue(submit.is_disabled())
        self.assertTrue(page.get_by_text("资料已修改；请勾选“显式重建快照”后再提交。", exact=True).is_visible())
        page.get_by_label("显式重建快照并重新扫描授权资源").check()
        self.assertFalse(submit.is_disabled())

        # 4. The rebuild confirmation lists the invalidation scope and cancel
        # sends no request.
        submit.click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("heading", name="确认重建资料快照？").wait_for()
        self.assertTrue(dialog.get_by_text("当前澄清问题", exact=False).is_visible())
        self.assertTrue(dialog.get_by_text("已填写的答案", exact=False).is_visible())
        dialog.get_by_role("button", name="取消").click()
        self.assertEqual(len(import_posts), 1)

        # 5. Confirm rebuild: new snapshot, clarification regenerates via model.
        submit.click()
        page.get_by_role("dialog").get_by_role("button", name="确认重建").click()
        page.get_by_role("heading", name="需求澄清", exact=True).wait_for()
        self.assertTrue(page.get_by_text("AI 生成问题", exact=True).is_visible())
        self.assertEqual(page.locator("fieldset.question-card").count(), 1)

        self.assertEqual(len(import_posts), 2)
        self.assertEqual(import_posts[0].post_data_json, {"source": SOURCE_A, "source_format": "markdown", "rebuild": False})
        self.assertEqual(import_posts[1].post_data_json, {"source": SOURCE_B, "source_format": "markdown", "rebuild": True})

        view = self.service.input_view("recover")
        self.assertEqual(view["source"], SOURCE_B)
        self.assertEqual(view["source_format"], "markdown")
        self.assertNotEqual(view["snapshot_hash"], first_snapshot)
        self.assertEqual(view["clarification"]["status"], "ready")
        self.assertEqual(view["clarification"]["question_source"], "model")
        current_snapshot = next(v for v in self.service.versions("recover", "input-snapshot") if v["hash"] == view["snapshot_hash"])
        current_input_hash = current_snapshot["metadata"]["raw_source_hash"]
        self.assertEqual(view["clarification"]["input_hash"], current_input_hash)
        clarification_event = [e for e in self.service.events("recover") if e["action"] == "clarification_generate"][-1]
        self.assertEqual(clarification_event["request_hash"], current_input_hash)
        self.assertEqual(clarification_event["result"]["snapshot_hash"], view["snapshot_hash"])
        self.assertEqual(len(self.service.versions("recover", "input-snapshot")), 2)
        self.assertEqual(self.clarifier.calls, 2)

        # 6. The rebuilt source is what future visits backfill.
        page.goto(self.base + "/tasks/recover?stage=created")
        page.get_by_role("heading", name="更新任务资料").wait_for()
        self.dismiss_resource_reminder(page)
        self.assertEqual(page.get_by_label("任务卡内容").input_value(), SOURCE_B)
        self.assertTrue(page.get_by_role("button", name="重建资料快照").is_disabled())

        self.assertEqual(errors, [])
        page.close()


if __name__ == "__main__":
    unittest.main()
