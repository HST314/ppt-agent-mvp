"""P0-3 regression: embedded previews must scale the fixed 1280×720 canvas to fit.

Before the fix the iframe was stretched to the container size while the deck
canvas stayed 1280px wide, so only 24%–80% of the canvas was visible. These
tests assert the scaled canvas exactly matches the container width and never
exceeds its bounds, on the sample, deck and review stages.
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


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(sync_playwright, "playwright is required")
class PreviewScaleToFitGate(unittest.TestCase):
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

    def _prepare_deck(self, task_id="scale-fit"):
        svc = self.service
        svc.create(task_id, "manual")
        svc.import_input(task_id, {"goal": "缩放验证", "audience": "管理层", "topic": "预览", "页数": 3})
        svc.generate_narrative(task_id)
        svc.confirm_narrative(task_id)
        svc.generate_outline(task_id)
        svc.confirm_outline(task_id)
        svc.generate_sample(task_id)
        svc.confirm_sample(task_id)
        svc.generate_deck(task_id)

    def test_sample_deck_review_previews_show_full_canvas_at_common_viewports(self):
        task_id = "scale-fit"
        self._prepare_deck(task_id)
        page = self.browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            for width in (375, 768, 1024, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                for stage, selector in (("sample", "#sample-preview"), ("deck", "#deck-preview-frame"), ("review", "#deck-preview-frame")):
                    page.goto(f"{self.base}/tasks/{task_id}?stage={stage}")
                    page.locator(f"{selector}").wait_for()
                    # 等 iframe 内容加载完成，保证缩放已按最终容器宽度应用
                    frame_element = page.locator(selector)
                    frame_element.element_handle().wait_for_element_state("stable")
                    metrics = page.evaluate(
                        """(sel) => {
                            const frame = document.querySelector(sel);
                            const aspect = frame.closest('.preview-aspect');
                            const a = aspect.getBoundingClientRect();
                            const f = frame.getBoundingClientRect();
                            return {aw: a.width, ah: a.height, al: a.left, ar: a.right,
                                    cw: aspect.clientWidth, ch: aspect.clientHeight,
                                    fw: f.width, fh: f.height, fl: f.left, fr: f.right};
                        }""",
                        selector,
                    )
                    # 画布缩放后必须与容器内容区同宽，且左右边界都落在容器内
                    self.assertAlmostEqual(metrics["fw"], metrics["cw"], delta=2.0, msg=f"{stage}@{width}: canvas width {metrics['fw']} != container content-box {metrics['cw']}")
                    self.assertGreaterEqual(metrics["fl"], metrics["al"] - 1.0, msg=f"{stage}@{width}: canvas overflows left edge")
                    self.assertLessEqual(metrics["fr"], metrics["ar"] + 1.0, msg=f"{stage}@{width}: canvas clipped at right edge")
                    # 16:9 画布等比缩放后高度与容器内容区一致，不再出现 4:3 / 16:10 变形
                    self.assertAlmostEqual(metrics["fh"], metrics["ch"], delta=2.0, msg=f"{stage}@{width}: scaled height mismatch")
                    self.assertAlmostEqual(metrics["fw"] / metrics["fh"], 16 / 9, delta=0.02, msg=f"{stage}@{width}: canvas no longer 16:9")
        finally:
            page.close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
