"""P1-1/P1-2 regression: the delivered file:// player must open cleanly offline.

P1-1: motion.min.js ships as a UMD classic script (window.Motion) so file://
no longer triggers module CORS errors. P1-2: the player scales the slide
canvas with min(vw/w, (vh-controls)/h) so small screens never clip the slide
and the controls always stay in a reserved band outside the canvas.
"""
import tempfile
import unittest

from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class PassingInspector:
    def inspect(self, outline, html): return {"passed": True, "issues": [], "model": "fixture"}


VIEWPORTS = (
    {"width": 375, "height": 667},
    {"width": 1024, "height": 768},
    {"width": 1280, "height": 720},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
)


@unittest.skipUnless(sync_playwright, "playwright is required")
class OfflinePlayerViewportGate(unittest.TestCase):
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
        store = WorkspaceStore(self.tmp.name)
        svc = TaskService(store, inspector=PassingInspector())
        svc.create("offline-viewports", "manual")
        svc.import_input("offline-viewports", {"goal": "发布", "audience": "客户", "topic": "离线演示", "页数": 3})
        svc.generate_narrative("offline-viewports")
        svc.confirm_narrative("offline-viewports")
        svc.generate_outline("offline-viewports")
        svc.confirm_outline("offline-viewports")
        svc.generate_sample("offline-viewports")
        svc.confirm_sample("offline-viewports")
        svc.generate_deck("offline-viewports")
        svc.run_inspection("offline-viewports", 0)
        deck = svc.deck_view("offline-viewports")["deck"]
        delivery = svc.confirm_delivery("offline-viewports", deck["hash"])["delivery"]
        self.root = store.delivery_root("offline-viewports", delivery["delivery_id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_player_scales_canvas_and_stays_clean_at_common_viewports(self):
        index_html = (self.root / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('type="module"', index_html, "file:// 下模块脚本会被 CORS 拦截，交付包不得再注入模块脚本")

        for viewport in VIEWPORTS:
            with self.subTest(viewport=viewport):
                page = self.browser.new_page(viewport=viewport)
                console_errors, page_errors, failed = [], [], []
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("requestfailed", lambda request: failed.append(request.url))
                page.route("http://**/*", lambda route: route.abort())
                page.route("https://**/*", lambda route: route.abort())
                try:
                    page.goto((self.root / "index.html").as_uri())
                    page.wait_for_selector('.slide[aria-hidden="false"]')
                    self.assertEqual(console_errors, [])
                    self.assertEqual(page_errors, [])
                    self.assertEqual(failed, [])
                    # UMD 版 Motion 以经典脚本加载成功
                    self.assertEqual(page.evaluate("typeof window.Motion"), "object")
                    self.assertEqual(page.evaluate("typeof window.Motion?.animate"), "function")
                    geometry = page.evaluate(
                        """() => {
                            const slide = document.querySelector('.slide[aria-hidden="false"]').getBoundingClientRect();
                            const controls = document.getElementById('offline-controls').getBoundingClientRect();
                            return {slide: {left: slide.left, right: slide.right, top: slide.top, bottom: slide.bottom},
                                    controls: {left: controls.left, right: controls.right, top: controls.top, bottom: controls.bottom},
                                    iw: window.innerWidth, ih: window.innerHeight,
                                    sw: document.documentElement.scrollWidth, sh: document.documentElement.scrollHeight};
                        }"""
                    )
                    slide, controls = geometry["slide"], geometry["controls"]
                    # 画布不被裁切：完整落在视口内，且页面没有可滚动溢出
                    self.assertLessEqual(geometry["sw"], geometry["iw"] + 1)
                    self.assertGreaterEqual(slide["left"], -1.0)
                    self.assertLessEqual(slide["right"], geometry["iw"] + 1.0)
                    self.assertGreaterEqual(slide["top"], -1.0)
                    self.assertLessEqual(slide["bottom"], geometry["ih"] + 1.0)
                    # 翻页控件位于画布外的保留区，不与画布重叠
                    overlap = not (
                        controls["bottom"] <= slide["top"] + 1.0
                        or controls["top"] >= slide["bottom"] - 1.0
                        or controls["right"] <= slide["left"] + 1.0
                        or controls["left"] >= slide["right"] - 1.0
                    )
                    self.assertFalse(overlap, f"controls overlap slide at {viewport}: {geometry}")
                    # 控件与键盘翻页在该视口下仍可用
                    self.assertEqual(page.locator("#offline-page").text_content(), "1 / 3")
                    page.click("#offline-next")
                    self.assertEqual(page.locator("#offline-page").text_content(), "2 / 3")
                    page.keyboard.press("End")
                    self.assertEqual(page.locator("#offline-page").text_content(), "3 / 3")
                    page.keyboard.press("Home")
                    self.assertEqual(page.locator("#offline-page").text_content(), "1 / 3")
                    self.assertEqual(console_errors, [])
                    self.assertEqual(page_errors, [])
                    self.assertEqual(failed, [])
                finally:
                    page.close()


if __name__ == "__main__":
    unittest.main()
