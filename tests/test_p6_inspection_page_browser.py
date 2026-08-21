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


class InspectionPageBrowserBase(unittest.TestCase):
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
        self.posts = []
        self.page.on("request", lambda request: self.posts.append(request) if request.method == "POST" else None)
        self.page.goto(self.base + "/tasks/task?stage=review")
        self.page.get_by_role("heading", name="自检与修改", exact=True).wait_for()

    def tearDown(self):
        self.page.close()
        self.server.should_exit = True
        self.thread.join(5)
        self.tmp.cleanup()


class InspectionPageBrowserTests(InspectionPageBrowserBase):
    inspector = Inspector()

    def test_locate_highlights_preview_and_batch_keeps_same_code(self):
        group = self.page.locator(".issue-group").filter(has_text="slide-1 · overflow")
        group.get_by_role("button", name="定位").click()
        frame = self.page.frame_locator("#deck-preview-frame")
        self.assertEqual(frame.locator('[data-element-id="title"]').first.get_attribute("data-inspection-highlight"), "true")
        self.assertIn("slide-1 / title", self.page.get_by_role("status").filter(has_text="已定位").inner_text())
        group.get_by_label("处置动作").select_option("defer")
        group.get_by_label("处置依据").fill("同类处理")
        with self.page.expect_request(lambda request: request.url.endswith("/issues/dispositions/batch")) as captured:
            group.get_by_role("button", name="处置本组 1 项").click()
        body = json.loads(captured.value.post_data)
        self.assertEqual(len(body["issue_ids"]), 1)
        self.assertRegex(body["issue_ids"][0], r"^inspection-[0-9a-f]{24}$")

    def test_issues_are_grouped_by_slide_and_code(self):
        groups = self.page.locator(".issue-group")
        groups.first.wait_for()
        self.assertEqual(groups.count(), 3)
        self.assertEqual(self.page.locator(".issue-group", has_text="slide-2 · overflow").count(), 1)
        self.assertEqual(self.page.locator(".issue-group", has_text="slide-2 · density").count(), 1)
        # 每个分组的处置表单是一份，而不是每卡片一份。
        first = groups.first
        self.assertEqual(first.get_by_label("处置动作").count(), 1)

    def test_issue_sources_and_content_addressed_evidence_are_visible(self):
        summary = self.page.get_by_role("heading", name="检查摘要", exact=True).locator("xpath=../../..").first
        self.assertIn("完整", summary.inner_text())
        issue = self.page.locator(".issue-card").first
        self.assertIn("semantic_model", issue.inner_text())
        self.assertNotIn("证据引用\n缺失", issue.inner_text())

    def test_mixed_severity_group_form_ids_unique_and_labels_resolve(self):
        groups = self.page.locator(".issue-group")
        groups.first.wait_for()
        self.assertEqual(groups.count(), 3)
        ids = self.page.eval_on_selector_all("select[id^='group-action-'], input[id^='group-rationale-']", "nodes => nodes.map((node) => node.id)")
        # 混合 blocker+warning：所有分组表单 ID 全局唯一，且带 blocker/warning 分区前缀。
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(ids), len(set(ids)))
        # 可读前缀保留分区与清洗后 slide/code，尾部摘要保证全局唯一。
        self.assertTrue(any(id_.startswith("group-action-blocker-slide-1-overflow-") for id_ in ids))
        self.assertTrue(any(id_.startswith("group-action-warning-slide-2-overflow-") for id_ in ids))
        # 每组 label 可达，且按 ID 解析回本组控件而非页面内同 ID 副本。
        for index in range(groups.count()):
            group = groups.nth(index)
            for label in ("处置动作", "处置依据"):
                control = group.get_by_label(label)
                self.assertEqual(control.count(), 1)
                self.assertTrue(control.evaluate("node => document.getElementById(node.id) === node"))


class SanitizeCollisionInspector:
    """code 契约是任意字符串：不同原值清洗后可能同形（空格与斜杠都归为 "-"）。"""

    def inspect(self, outline, html):
        return {"passed": False, "issues": [
            {"issue_id": "space-code", "severity": "warning", "level": "element", "code": "text overflow", "message": "文本溢出", "slide_id": "slide-2", "element_id": "title", "evidence": "越界", "suggestion": "缩小"},
            {"issue_id": "slash-code", "severity": "warning", "level": "element", "code": "text/overflow", "message": "文本溢出", "slide_id": "slide-2", "element_id": "title", "evidence": "越界", "suggestion": "缩小"},
        ]}


class InspectionPageSanitizeCollisionTests(InspectionPageBrowserBase):
    inspector = SanitizeCollisionInspector()

    def test_sanitize_collision_group_ids_unique_and_labels_resolve(self):
        groups = self.page.locator(".issue-group")
        groups.first.wait_for()
        # "text overflow" 与 "text/overflow" 清洗后同形，但原始 code 不同，必须保留为两个独立分组。
        self.assertEqual(groups.count(), 2)
        ids = self.page.eval_on_selector_all("select[id^='group-action-'], input[id^='group-rationale-']", "nodes => nodes.map((node) => node.id)")
        # 两组共享同形可读前缀，全局唯一性完全依赖未清洗原值的稳定摘要。
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(id_.startswith(("group-action-warning-slide-2-text-overflow-", "group-rationale-warning-slide-2-text-overflow-")) for id_ in ids))
        # 逐组 label 可达，且按 ID 解析回本组控件而非页面内同 ID 副本。
        for index in range(groups.count()):
            group = groups.nth(index)
            for label in ("处置动作", "处置依据"):
                control = group.get_by_label(label)
                self.assertEqual(control.count(), 1)
                self.assertTrue(control.evaluate("node => document.getElementById(node.id) === node"))


if __name__ == "__main__":
    unittest.main()
