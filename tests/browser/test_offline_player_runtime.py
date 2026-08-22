"""离线交付受控演示 runtime 的真实浏览器验收。

覆盖真实 API 审计发现的交互缺口:滚轮/触摸翻页、ESC 索引视图、B 低功耗、
data-anim 动效执行、pipeline 手动推进、prefers-reduced-motion 默认静态、
文档标题兜底,以及封面 .h-hero 不再被服务端兜底规则压平
(1280 画布钉定 152px,对 18px lead 保持 ≥8:1 主标题对比)。
"""
import re
import tempfile
import unittest
from pathlib import Path

from ppt_agent.offline import offline_assets, offline_player
from ppt_agent.p4 import assemble_locked_template

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


SLIDES = [
    '<section class="slide dark hero accent" id="s1" data-slide-id="s1" data-animate="hero">'
    '<div class="kicker" data-anim="kicker">FIELD NOTE</div>'
    '<h1 data-element-id="title" class="h-hero" data-anim="title">离线运行时验收</h1>'
    '<p class="lead" data-anim="lead">受控演示 runtime 专项</p></section>',
    '<section class="slide" id="s2" data-slide-id="s2" data-animate="pipeline">'
    '<h2 data-element-id="title" data-anim="headline">推进路线</h2>'
    '<div class="pipeline" data-cols="3">'
    '<div class="step" data-anim="step"><div class="step-nb">01</div><div class="step-title">试点</div></div>'
    '<div class="step" data-anim="step"><div class="step-nb">02</div><div class="step-title">扩容</div></div>'
    '<div class="step" data-anim="step"><div class="step-nb">03</div><div class="step-title">推广</div></div>'
    '</div></section>',
    '<section class="slide light" id="s3" data-slide-id="s3" data-animate="cascade">'
    '<h2 data-element-id="title" data-anim="t">总结</h2>'
    '<p data-anim="p1">第一要点</p><p data-anim="p2">第二要点</p></section>',
]


def build_player(root: Path, deck_html: str | None = None) -> Path:
    deck = deck_html if deck_html is not None else assemble_locked_template(SLIDES)
    (root / "index.html").write_text(offline_player(deck), encoding="utf-8")
    for relative, data in offline_assets().items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root / "index.html"


def swipe_left(page):
    page.evaluate(
        """() => {
            const target = document.body;
            const touch = (x, y) => new Touch({identifier: 1, target, clientX: x, clientY: y});
            target.dispatchEvent(new TouchEvent('touchstart', {touches: [touch(600, 360)], bubbles: true}));
            target.dispatchEvent(new TouchEvent('touchend', {changedTouches: [touch(200, 360)], bubbles: true}));
        }"""
    )


def swipe_right(page):
    page.evaluate(
        """() => {
            const target = document.body;
            const touch = (x, y) => new Touch({identifier: 1, target, clientX: x, clientY: y});
            target.dispatchEvent(new TouchEvent('touchstart', {touches: [touch(200, 360)], bubbles: true}));
            target.dispatchEvent(new TouchEvent('touchend', {changedTouches: [touch(600, 360)], bubbles: true}));
        }"""
    )


@unittest.skipUnless(sync_playwright, "playwright is required")
class OfflinePlayerRuntimeGate(unittest.TestCase):
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
        self.index = build_player(Path(self.tmp.name))
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 720})
        self.page = self.context.new_page()
        self.console_errors, self.page_errors = [], []
        self.page.on("console", lambda message: self.console_errors.append(message.text) if message.type == "error" else None)
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.console_errors, [])
        self.assertEqual(self.page_errors, [])
        self.context.close()
        self.tmp.cleanup()

    def open_player(self, fragment=""):
        self.page.goto(self.index.as_uri() + fragment)
        self.page.wait_for_selector('.slide[aria-hidden="false"]')

    def indicator(self):
        return self.page.locator("#offline-page").text_content()

    def step_opacities(self):
        return self.page.evaluate(
            """() => [...document.querySelectorAll('#s2 [data-anim="step"]')]
                .map(el => parseFloat(getComputedStyle(el).opacity))"""
        )

    def test_cover_title_not_squashed_and_document_title(self):
        self.open_player()
        self.assertEqual(self.page.title(), "离线运行时验收")
        sizes = self.page.evaluate(
            """() => {
                const size = sel => parseFloat(getComputedStyle(document.querySelector(sel)).fontSize);
                return {hero: size('#s1 .h-hero'), lead: size('#s1 .lead'), plain: size('#s3 [data-element-id="title"]')};
            }"""
        )
        # 默认 editorial 样式的 .h-hero 服务端 pin 为 148px（swiss 152px 由单测覆盖）
        self.assertEqual(sizes["hero"], 148.0)
        self.assertEqual(sizes["lead"], 18.0)
        self.assertGreaterEqual(sizes["hero"] / sizes["lead"], 8.0)
        # 无展示类的普通标题仍走 52px 服务端兜底
        self.assertEqual(sizes["plain"], 52.0)

    def test_data_anim_executes_with_motion(self):
        self.open_player()
        self.assertIn("motion-ready", self.page.get_attribute("body", "class") or "")
        self.page.wait_for_timeout(1600)
        opacities = self.page.evaluate(
            """() => [...document.querySelectorAll('#s1 [data-anim]')]
                .map(el => parseFloat(getComputedStyle(el).opacity))"""
        )
        self.assertTrue(opacities)
        self.assertTrue(all(value == 1.0 for value in opacities), opacities)

    def test_wheel_and_touch_navigation(self):
        self.open_player()
        self.assertEqual(self.indicator(), "1 / 3")
        self.page.mouse.wheel(0, 120)
        self.page.wait_for_timeout(150)
        self.assertEqual(self.indicator(), "2 / 3")
        # 滚轮回退不被 pipeline 拦截
        self.page.mouse.wheel(0, -120)
        self.page.wait_for_timeout(150)
        self.assertEqual(self.indicator(), "1 / 3")
        swipe_left(self.page)
        self.page.wait_for_timeout(150)
        self.assertEqual(self.indicator(), "2 / 3")
        swipe_right(self.page)
        self.page.wait_for_timeout(150)
        self.assertEqual(self.indicator(), "1 / 3")

    def test_pipeline_manual_advance_before_page_turn(self):
        self.open_player("#slide=2")
        self.assertEqual(self.indicator(), "2 / 3")
        self.page.wait_for_timeout(300)
        # 进入 pipeline 页: 步骤先全部压暗
        self.assertEqual(self.step_opacities(), [0.15, 0.15, 0.15])
        # 非步骤元素(标题)正常入场
        self.page.wait_for_timeout(900)
        headline_opacity = self.page.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('#s2 [data-anim=\"headline\"]')).opacity)"
        )
        self.assertEqual(headline_opacity, 1.0)
        # 前三次前进只点亮步骤,不翻页
        for step in range(3):
            self.page.keyboard.press("ArrowRight")
            self.assertEqual(self.indicator(), "2 / 3")
            self.page.wait_for_timeout(700)
            expected = [1.0] * (step + 1) + [0.15] * (2 - step)
            self.assertEqual(self.step_opacities(), expected)
        # 全部亮起后才翻页
        self.page.keyboard.press("ArrowRight")
        self.assertEqual(self.indicator(), "3 / 3")

    def test_pipeline_wheel_advances_steps(self):
        self.open_player("#slide=2")
        self.page.wait_for_timeout(300)
        self.page.mouse.wheel(0, 120)
        self.page.wait_for_timeout(700)
        self.assertEqual(self.indicator(), "2 / 3")
        self.assertEqual(self.step_opacities(), [1.0, 0.15, 0.15])

    def test_esc_overview_opens_jumps_and_guards_keys(self):
        self.open_player()
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector("#overview.open")
        self.assertEqual(self.page.locator("#overview .offline-card").count(), 3)
        # 索引打开时翻页键被拦截
        self.page.keyboard.press("ArrowRight")
        self.page.wait_for_timeout(150)
        self.assertEqual(self.indicator(), "1 / 3")
        # 点击卡片跳转并关闭索引
        self.page.locator("#overview .offline-card").nth(2).click()
        self.page.wait_for_timeout(150)
        self.assertEqual(self.indicator(), "3 / 3")
        self.assertEqual(self.page.locator("#overview.open").count(), 0)
        # 再次 ESC 打开, ESC 关闭
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector("#overview.open")
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(150)
        self.assertEqual(self.page.locator("#overview.open").count(), 0)

    def test_low_power_toggle_persists_and_reveals_static(self):
        self.open_player()
        self.page.keyboard.press("b")
        self.page.wait_for_timeout(150)
        self.assertIn("low-power", self.page.get_attribute("body", "class") or "")
        self.assertEqual(self.page.evaluate("localStorage.getItem('guizang-ppt-low-power')"), "1")
        self.assertIn("动态", self.page.locator("#offline-hint").text_content())
        # 静态模式下所有待动画元素立即可见
        opacities = self.page.evaluate(
            """() => [...document.querySelectorAll('#s1 [data-anim]')]
                .map(el => parseFloat(getComputedStyle(el).opacity))"""
        )
        self.assertTrue(all(value == 1.0 for value in opacities), opacities)
        # 刷新后保持( localStorage 持久化 )
        self.page.reload()
        self.page.wait_for_selector('.slide[aria-hidden="false"]')
        self.assertIn("low-power", self.page.get_attribute("body", "class") or "")
        # 再按 B 恢复动态
        self.page.keyboard.press("b")
        self.page.wait_for_timeout(150)
        self.assertNotIn("low-power", self.page.get_attribute("body", "class") or "")
        self.assertEqual(self.page.evaluate("localStorage.getItem('guizang-ppt-low-power')"), "0")

    def test_reduced_motion_defaults_to_static(self):
        context = self.browser.new_context(viewport={"width": 1280, "height": 720}, reduced_motion="reduce")
        page = context.new_page()
        try:
            page.goto(self.index.as_uri())
            page.wait_for_selector('.slide[aria-hidden="false"]')
            self.assertIn("low-power", page.get_attribute("body", "class") or "")
            opacities = page.evaluate(
                """() => [...document.querySelectorAll('#s1 [data-anim]')]
                    .map(el => parseFloat(getComputedStyle(el).opacity))"""
            )
            self.assertTrue(all(value == 1.0 for value in opacities), opacities)
        finally:
            page.close()
            context.close()

    def test_document_title_fallback_when_server_title_missing(self):
        deck = assemble_locked_template(SLIDES)
        stripped = re.sub(r"<title>[\s\S]*?</title>", "", deck, count=1)
        self.assertNotIn("<title>", stripped)
        index = build_player(Path(self.tmp.name), stripped)
        self.page.goto(index.as_uri())
        self.page.wait_for_selector('.slide[aria-hidden="false"]')
        self.assertEqual(self.page.title(), "离线运行时验收")


if __name__ == "__main__":
    unittest.main()
