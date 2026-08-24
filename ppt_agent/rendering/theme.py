from __future__ import annotations

import re

from ..generation.contracts import ThemeTokens
from ..generation.errors import ContractValidationError, ErrorContext


FONT_PATTERN = re.compile(r"^[\w\- ,.'\u3400-\u9fff]{1,128}$", re.UNICODE)


def css_theme(theme: ThemeTokens) -> str:
    """Serialize frozen design tokens without accepting executable CSS."""
    for name, value in (("font_heading", theme.font_heading), ("font_body", theme.font_body)):
        if not FONT_PATTERN.fullmatch(value):
            raise ContractValidationError("字体令牌包含不安全字符", context=ErrorContext(field_path=f"theme.{name}"))
    heading = _font_stack(theme.font_heading)
    body = _font_stack(theme.font_body)
    return (
        ":root{"
        f"--background:{theme.background};"
        f"--surface:{theme.surface};"
        f"--text:{theme.text};"
        f"--muted-text:{theme.muted_text};"
        f"--primary:{theme.primary};"
        f"--accent:{theme.accent};"
        f"--font-heading:{heading};"
        f"--font-body:{body};"
        f"--radius:{theme.border_radius}px;"
        f"--space:{theme.space_unit}px"
        "}"
    )


def _font_stack(value: str) -> str:
    names = [item.strip() for item in value.split(",") if item.strip()]
    return ",".join(f"'{name}'" for name in names) + ",sans-serif"
