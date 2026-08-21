from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any

from .claim_ledger import audit_html_claims, validate_claim_ledger
from .design_contract import validate_design_contract
from .errors import ValidationError


_OVERFLOW_CODES = {"content_out_of_bounds", "element_scroll_overflow", "canvas_size", "slide_sequence_mismatch"}
_HARD_BROWSER_CODES = _OVERFLOW_CODES | {"render_unavailable", "invalid_measurement", "empty_slide", "broken_image", "missing_title", "title_too_small", "text_too_small"}


def canonical_post_render_evidence(evidence: dict[str, Any]) -> bytes:
    """Serialize the complete evidence body without its self-reference."""
    if not isinstance(evidence, dict):
        raise ValidationError("渲染后门禁 evidence 格式无效")
    body = {key: value for key, value in evidence.items() if key != "evidence_hash"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def post_render_evidence_hash(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_post_render_evidence(evidence)).hexdigest()


def seal_post_render_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in evidence.items() if key != "evidence_hash"}
    return {**body, "evidence_hash": post_render_evidence_hash(body)}


class _ContractParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.slides: list[dict[str, Any]] = []
        self.contract_hashes: list[str] = []
        self.template_values: list[str] = []
        self._active: dict[str, Any] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        values = {str(name).lower(): value or "" for name, value in attrs}
        if tag.lower() == "meta" and values.get("name") == "design-contract":
            self.contract_hashes.append(values.get("content", ""))
        if tag.lower() == "meta" and values.get("name") == "ppt-template":
            self.template_values.append(values.get("content", ""))
        classes = values.get("class", "").split()
        if tag.lower() == "section" and "slide" in classes and self._active is None:
            self._active = {
                "slide_id": values.get("data-slide-id") or values.get("id") or "",
                "layout_id": values.get("data-layout", ""),
                "contract_hash": values.get("data-contract-hash", ""),
                "classes": classes,
                "animation_recipe": values.get("data-animate", ""),
                "animation_markers": 0,
            }
            self._depth = 1
            return
        if self._active is not None:
            if tag.lower() == "section":
                self._depth += 1
            if "data-anim" in values:
                self._active["animation_markers"] += 1

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if self._active is None or tag.lower() != "section":
            return
        self._depth -= 1
        if self._depth == 0:
            self.slides.append(self._active)
            self._active = None


def _theme_classes(theme: str) -> set[str]:
    return set(theme.split("-")) if theme else set()


def inspect_contract(html_text: str, expected_slide_ids: list[str], contract: dict[str, Any], contract_hash: str) -> dict[str, Any]:
    validate_design_contract(contract)
    parser = _ContractParser()
    parser.feed(html_text)
    parser.close()
    expected = {item["slide_id"]: item for item in contract["slide_contracts"]}
    issues = []
    actual_ids = [slide["slide_id"] for slide in parser.slides]
    if actual_ids != expected_slide_ids:
        issues.append({"code": "contract_slide_sequence", "evidence": f"expected={expected_slide_ids}; actual={actual_ids}"})
    if parser.contract_hashes != [contract_hash]:
        issues.append({"code": "contract_hash_mismatch", "evidence": f"embedded={parser.contract_hashes}; expected={contract_hash}"})
    if len(parser.template_values) != 1 or contract["template_id"] not in parser.template_values[0] or contract["template_hash"] not in parser.template_values[0]:
        issues.append({"code": "template_provenance_mismatch", "evidence": "模板 provenance 与 DesignContract 不一致"})
    registered = 0
    for slide in parser.slides:
        item = expected.get(slide["slide_id"])
        if item is None:
            continue
        if slide["layout_id"] != item["layout_id"] or slide["layout_id"] not in contract["allowed_layouts"]:
            issues.append({"code": "layout_not_registered", "slide_id": slide["slide_id"], "evidence": f"actual={slide['layout_id']}; expected={item['layout_id']}"})
        else:
            registered += 1
        if slide["contract_hash"] != contract_hash:
            issues.append({"code": "slide_contract_hash_mismatch", "slide_id": slide["slide_id"], "evidence": f"actual={slide['contract_hash']}; expected={contract_hash}"})
        required_theme = _theme_classes(item["theme"])
        if not required_theme.issubset(set(slide["classes"])):
            issues.append({"code": "theme_contract_mismatch", "slide_id": slide["slide_id"], "evidence": f"actual={slide['classes']}; expected={sorted(required_theme)}"})
        if slide["animation_recipe"] != item["animation_recipe"]:
            issues.append({"code": "animation_recipe_mismatch", "slide_id": slide["slide_id"], "evidence": f"actual={slide['animation_recipe']}; expected={item['animation_recipe']}"})
        if slide["animation_markers"] < item["minimum_animation_markers"]:
            issues.append({"code": "animation_markers_missing", "slide_id": slide["slide_id"], "evidence": f"actual={slide['animation_markers']}; minimum={item['minimum_animation_markers']}"})
    return {
        "passed": not issues,
        "issues": issues,
        "total_slides": len(expected_slide_ids),
        "registered_slides": registered,
        "layout_registration_percent": round(registered * 100 / len(expected_slide_ids), 2) if expected_slide_ids else 0,
    }


def run_post_render_gate(
    html_text: str,
    *,
    expected_slide_ids: list[str],
    contract: dict[str, Any],
    contract_hash: str,
    claim_ledger: dict[str, Any],
    claim_ledger_hash: str,
    browser_inspector=None,
    overflow_autofit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_claim_ledger(claim_ledger)
    structure = inspect_contract(html_text, expected_slide_ids, contract, contract_hash)
    claims = audit_html_claims(html_text, claim_ledger)
    browser = None
    browser_blockers = []
    if browser_inspector is not None:
        browser = browser_inspector.inspect(html_text, expected_slide_ids)
        for issue in browser.get("issues", []) if isinstance(browser, dict) else []:
            if issue.get("severity") == "blocker" or issue.get("code") in _HARD_BROWSER_CODES:
                browser_blockers.append(issue)
        if not isinstance(browser, dict) or not browser.get("available"):
            if not any(item.get("code") == "render_unavailable" for item in browser_blockers):
                browser_blockers.append({"code": "render_unavailable", "severity": "blocker", "evidence": "Chromium 未返回可用测量"})
        elif not browser.get("passed") and not browser_blockers:
            browser_blockers.append({"code": "invalid_measurement", "severity": "blocker", "evidence": "Chromium 测量失败但未返回问题"})
    blockers = [
        *[{"source": "design_contract", **item} for item in structure["issues"]],
        *[{
            "source": "claim_ledger", "code": "unbound_claim", "evidence": item["value"],
        } for item in claims["unbound"]],
        *[{"source": "chromium", **item} for item in browser_blockers],
    ]
    overflow = [item for item in browser_blockers if item.get("code") in _OVERFLOW_CODES]
    evidence = {
        "passed": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "design_contract_hash": contract_hash,
        "claim_ledger_hash": claim_ledger_hash,
        "rendered_html_hash": hashlib.sha256(html_text.encode()).hexdigest(),
        "layout": structure,
        "claims": {
            "binding_count": claims["binding_count"],
            "unbound_count": claims["unbound_count"],
            "unbound": claims["unbound"],
            "text_hash": claims["text_hash"],
        },
        "geometry": {
            "available": browser is not None and bool(browser.get("available")),
            "passed": None if browser is None else bool(browser.get("passed")) and not browser_blockers,
            "overflow_count": len(overflow),
            "blocker_count": len(browser_blockers),
            "engine": None if browser is None else browser.get("engine"),
            "engine_version": None if browser is None else browser.get("engine_version"),
            "viewport": None if browser is None else browser.get("viewport"),
        },
        "overflow_autofit": overflow_autofit,
    }
    return seal_post_render_evidence(evidence)


def enforce_post_render_gate(*args, **kwargs) -> dict[str, Any]:
    evidence = run_post_render_gate(*args, **kwargs)
    if evidence["blockers"]:
        summary = "；".join(f"{item.get('code')}:{item.get('evidence', '')}" for item in evidence["blockers"][:5])
        raise ValidationError(f"渲染后硬门禁未通过：{summary}")
    return evidence
