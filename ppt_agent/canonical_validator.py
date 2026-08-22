from __future__ import annotations

from .layout_structure import run_layout_structure_validator


def run_canonical_validator(html_text: str, design_language: str | None = None, *, timeout_seconds: float = 45) -> dict:
    """Compatibility advisory; framework generation has no canonical design DOM."""
    return {
        "applicable": False,
        "passed": True,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "errors": [],
        "script_hash": None,
        "timeout_seconds": timeout_seconds,
        "structural_signatures": run_layout_structure_validator(html_text, design_language),
    }
