from __future__ import annotations


def run_layout_structure_validator(_html_text: str, _design_language: str | None = None) -> dict:
    """Compatibility advisory after layout vocabulary moved into Agent Skills."""
    return {
        "advisory": True,
        "applicable": False,
        "passed": True,
        "checked_slide_count": 0,
        "matched_slide_count": 0,
        "slides": [],
        "errors": [],
    }
