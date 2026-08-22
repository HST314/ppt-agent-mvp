from __future__ import annotations

from typing import Any

from .render_gate import TechnicalGate


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
    """Compatibility adapter backed by the single TechnicalGate classifier."""
    return TechnicalGate.browser_blockers(evidence)
