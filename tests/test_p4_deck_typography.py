"""Generic presentation shell and Agent-owned typography regression tests."""

import unittest

from ppt_agent.design_contract import build_presentation_technical_contract
from ppt_agent.p2 import canonical, digest
from ppt_agent.p4 import assemble_presentation, validate_html


def _slide(inner, slide_id="s1", classes="slide custom-layout"):
    return f'<section class="{classes}" id="{slide_id}" data-slide-id="{slide_id}">{inner}</section>'


def _contract():
    return build_presentation_technical_contract(
        task_id="task-1",
        input_snapshot_hash="a" * 64,
        outline_hash="b" * 64,
        slide_ids=["s1"],
        created_at="2026-01-01T00:00:00Z",
    )


class GenericShellTypographyTests(unittest.TestCase):
    def test_agent_shared_css_controls_typography_and_classes_are_preserved(self):
        contract = _contract()
        contract_hash = digest(canonical(contract))
        css = ".custom-layout{display:grid}.display-title{font:800 88px/1.05 Georgia;color:#5b21b6}"
        html = assemble_presentation(
            [_slide('<h1 class="display-title">季度<em>复盘</em></h1>')],
            technical_contract=contract,
            contract_hash=contract_hash,
            design_intent={
                "style_summary": "editorial",
                "color_strategy": "violet on white",
                "typography_strategy": "serif display",
                "layout_principles": ["asymmetric grid"],
                "rationale": "match the brief",
            },
            shared_assets={"css": css},
        )
        self.assertIn(css, html)
        self.assertIn('class="slide custom-layout"', html)
        self.assertNotIn("data-layout=", html)
        validate_html(html, ["s1"])

    def test_shell_has_accessibility_defaults_without_style_vocabulary(self):
        html = assemble_presentation([_slide("<h1>T</h1>")])
        self.assertIn("@media (prefers-reduced-motion:reduce)", html)
        self.assertIn(":focus-visible", html)
        self.assertNotIn("template-swiss", html)


class DeckTitleTests(unittest.TestCase):
    def test_title_from_first_h1_and_validates(self):
        html = assemble_presentation([_slide('<h1 data-element-id="title">季度<em>复盘</em></h1>')])
        self.assertIn("<title>季度复盘</title>", html)
        validate_html(html, ["s1"])

    def test_title_from_marked_element_and_entities_unwrapped(self):
        html = assemble_presentation([_slide('<h2 data-element-id="title">标&nbsp;题</h2>')])
        self.assertIn("<title>标 题</title>", html)

    def test_title_fallback(self):
        html = assemble_presentation([_slide("<p>无标题</p>")])
        self.assertIn("<title>演示文稿</title>", html)


if __name__ == "__main__":
    unittest.main()
