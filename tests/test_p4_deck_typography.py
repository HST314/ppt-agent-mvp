"""生成页排版与主题完整性回归(真实 API 审计发现的修复证据)。

- 兜底标题规则不得再压平模板展示类(.h-hero 曾被全局 52px 覆盖,封面比仅 2.17:1);
- 展示类按 1280×720 画布钉定 px,主标题对 18px lead 保持 ≥8:1;
- Swiss 补齐 layouts.md 版式词汇(rowline/pipeline/step-*/split-55);
- 内联样式禁止重定义锁定主题变量(IKB 单主题),布局级变量不受影响;
- 装配产物必须带 <title>。
"""
import unittest

from ppt_agent.errors import ValidationError
from ppt_agent.p4 import (
    LOCKED_THEME_TOKENS,
    assemble_locked_template,
    locked_template,
    validate_html,
)


def _slide(inner, slide_id="s1"):
    return f'<section class="slide" id="{slide_id}" data-slide-id="{slide_id}">{inner}</section>'


class LockedTemplateTypographyTests(unittest.TestCase):
    def test_swiss_display_classes_pinned_and_title_rule_guarded(self):
        style = locked_template("swiss")["style"]
        guard = ":not(.h-hero,.h-hero-zh,.h-xl,.h-xl-zh,.h-md,.h-sub)"
        self.assertIn(f".slide>h1{guard}", style)
        self.assertIn(f".slide>h2{guard}", style)
        self.assertIn(f'.slide [data-element-id="title"]{guard}', style)
        for pin in (
            ".slide .h-hero{font-size:152px}",
            ".slide .h-hero-zh{font-size:148px}",
            ".slide .h-xl{font-size:77px}",
            ".slide .h-xl-zh{font-size:64px}",
            ".slide .lead{font-size:18px}",
            ".slide .kpi-hero{font-size:282px}",
            ".slide .kpi-big{font-size:141px}",
            ".slide .kpi-thin{font-size:179px}",
            ".slide .kpi-thin-sm{font-size:72px}",
            ".slide .num-mega,.slide .name-mega{font-size:115px}",
        ):
            self.assertIn(pin, style)

    def test_swiss_missing_layout_classes_now_defined(self):
        style = locked_template("swiss")["style"]
        for selector in (
            ".rowline{", ".rowline .k{", ".rowline .v{", ".rowline .m{",
            ".pipeline-section{", ".pipeline-label{", ".pipeline{", '.pipeline[data-cols="3"]',
            ".step{", ".step-nb{", ".step-title{", ".step-desc{", ".step-meta{",
            ".split-55{",
        ):
            self.assertIn(selector, style)
        # 字号下限对齐清单: 正文≥18 / 描述≥16 / meta≥14
        for pin in (
            ".slide .rowline .k{font-size:18px}",
            ".slide .rowline .v{font-size:16px}",
            ".slide .rowline .m{font-size:14px}",
            ".slide .step-title{font-size:18px}",
            ".slide .step-desc{font-size:16px}",
            ".slide .step-meta{font-size:14px}",
            ".slide .pipeline-label{font-size:14px}",
        ):
            self.assertIn(pin, style)

    def test_editorial_display_classes_pinned_and_title_rule_guarded(self):
        style = locked_template("editorial")["style"]
        guard = ":not(.display,.display-zh,.h1-zh,.h2-zh,.h3-zh,.h-hero,.h-xl,.h-sub,.h-md)"
        self.assertIn(f".slide>h1{guard}", style)
        for pin in (
            ".slide .display{font-size:152px}",
            ".slide .display-zh{font-size:148px}",
            ".slide .h-hero{font-size:148px}",
            ".slide .lead{font-size:18px}",
        ):
            self.assertIn(pin, style)

    def test_deck_style_carries_accessibility_rules(self):
        for style_id in ("editorial", "swiss"):
            style = locked_template(style_id)["style"]
            self.assertIn("@media (prefers-reduced-motion:reduce)", style)
            self.assertIn(":focus-visible", style)


class DeckTitleTests(unittest.TestCase):
    def test_title_from_first_h1_and_validates(self):
        html = assemble_locked_template([_slide('<h1 data-element-id="title" class="h-hero">季度<em>复盘</em></h1>')])
        self.assertIn("<title>季度复盘</title>", html)
        validate_html(html, ["s1"])

    def test_title_from_marked_element_and_entities_unwrapped(self):
        html = assemble_locked_template([_slide('<h2 data-element-id="title">标&nbsp;题</h2>')])
        self.assertIn("<title>标 题</title>", html)

    def test_title_fallback(self):
        html = assemble_locked_template([_slide("<p>无标题</p>")])
        self.assertIn("<title>演示文稿</title>", html)


class InlineThemeTokenTests(unittest.TestCase):
    def test_inline_accent_override_rejected(self):
        fragment = _slide('<h1 data-element-id="title">T</h1><p style="--accent:#18b6c9">x</p>')
        with self.assertRaisesRegex(ValidationError, "锁定主题变量"):
            validate_html(assemble_locked_template([fragment]), ["s1"])

    def test_every_locked_theme_token_rejected_inline(self):
        for token in sorted(LOCKED_THEME_TOKENS):
            with self.subTest(token=token):
                fragment = _slide(f'<h1 data-element-id="title">T</h1><p style="{token}:red">x</p>')
                with self.assertRaises(ValidationError):
                    validate_html(assemble_locked_template([fragment]), ["s1"])

    def test_layout_scoped_var_still_allowed_inline(self):
        fragment = _slide('<h1 data-element-id="title">T</h1><div style="--cols:3">x</div>')
        validate_html(assemble_locked_template([fragment]), ["s1"])

    def test_template_style_block_theme_tokens_unaffected(self):
        # 锁定模板自身 :root{--accent:#002FA7...} 走 <style> 块路径,不得误伤
        html = assemble_locked_template([_slide('<h1 data-element-id="title">T</h1>')])
        validate_html(html, ["s1"])


if __name__ == "__main__":
    unittest.main()
