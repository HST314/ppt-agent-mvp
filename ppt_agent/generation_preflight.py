from __future__ import annotations

from typing import Any


_HARD_BROWSER_CODES = {
    "content_out_of_bounds",
    "slide_scroll_overflow",
    "element_scroll_overflow",
    "render_unavailable",
    "invalid_measurement",
    "empty_slide",
    "broken_image",
    "missing_title",
    "title_too_small",
    "text_too_small",
}


def layout_capacity_policy(contract: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Compatibility view for the retired framework-owned layout budgets.

    PresentationTechnicalContract deliberately has no visual roles, template
    identifiers, card quotas or copy-density policy.  A Skill may publish those
    constraints as design guidance, but the generic framework must not invent
    or enforce them.
    """
    del contract
    return {}


def inspect_layout_capacity(html_text: str, contract: dict[str, Any]) -> dict[str, Any]:
    """Return an explicit non-applicable result for the retired style gate."""
    del html_text, contract
    return {
        "passed": True,
        "applicable": False,
        "issues": [],
        "reason": "layout capacity is owned by DesignIntent and the active Skill",
    }


def structured_canonical_blockers(
    validation: dict[str, Any], contract: dict[str, Any]
) -> list[dict[str, Any]]:
    """Compatibility adapter: canonical style validation is no longer a gate."""
    del validation, contract
    return []


def hard_browser_blockers(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract objective rendering failures from browser measurements."""
    if not isinstance(evidence, dict):
        return [{"code": "invalid_measurement", "evidence": "浏览器预检未返回对象"}]
    issues = evidence.get("issues") if isinstance(evidence.get("issues"), list) else []
    blockers = [
        item
        for item in issues
        if item.get("severity") == "blocker" or item.get("code") in _HARD_BROWSER_CODES
    ]
    if not evidence.get("available") and not any(
        item.get("code") == "render_unavailable" for item in blockers
    ):
        blockers.append({"code": "render_unavailable", "evidence": "Chromium 预检不可用"})
    if evidence.get("available") and not evidence.get("passed") and not blockers:
        blockers.append(
            {"code": "invalid_measurement", "evidence": "Chromium 预检失败但没有结构化问题"}
        )
    return blockers
