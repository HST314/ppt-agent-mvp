from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any

from .claim_ledger import audit_html_claims, audit_html_claims_by_slide, validate_claim_ledger
from .design_contract import validate_presentation_technical_contract
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
        self._active: dict[str, Any] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        values = {str(name).lower(): value or "" for name, value in attrs}
        if tag.lower() == "meta" and values.get("name") == "presentation-technical-contract":
            self.contract_hashes.append(values.get("content", ""))
        classes = values.get("class", "").split()
        if tag.lower() == "section" and "slide" in classes and self._active is None:
            self._active = {
                "slide_id": values.get("data-slide-id") or values.get("id") or "",
                "contract_hash": values.get("data-contract-hash", ""),
            }
            self._depth = 1
            return
        if self._active is not None:
            if tag.lower() == "section":
                self._depth += 1

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if self._active is None or tag.lower() != "section":
            return
        self._depth -= 1
        if self._depth == 0:
            self.slides.append(self._active)
            self._active = None


def inspect_contract(html_text: str, expected_slide_ids: list[str], contract: dict[str, Any], contract_hash: str) -> dict[str, Any]:
    validate_presentation_technical_contract(contract)
    parser = _ContractParser()
    parser.feed(html_text)
    parser.close()
    expected = set(contract["slide_ids"])
    issues = []
    actual_ids = [slide["slide_id"] for slide in parser.slides]
    if actual_ids != expected_slide_ids:
        issues.append({"code": "contract_slide_sequence", "evidence": f"expected={expected_slide_ids}; actual={actual_ids}"})
    if parser.contract_hashes != [contract_hash]:
        issues.append({"code": "contract_hash_mismatch", "evidence": f"embedded={parser.contract_hashes}; expected={contract_hash}"})
    registered = 0
    for slide in parser.slides:
        if slide["slide_id"] not in expected:
            continue
        registered += 1
        if slide["contract_hash"] != contract_hash:
            issues.append({"code": "slide_contract_hash_mismatch", "slide_id": slide["slide_id"], "evidence": f"actual={slide['contract_hash']}; expected={contract_hash}"})
    return {
        "passed": not issues,
        "issues": issues,
        "total_slides": len(expected_slide_ids),
        "registered_slides": registered,
        "layout_registration_percent": round(registered * 100 / len(expected_slide_ids), 2) if expected_slide_ids else 0,
        "technical_binding_percent": round(registered * 100 / len(expected_slide_ids), 2) if expected_slide_ids else 0,
    }


def run_post_render_gate(
    html_text: str,
    *,
    expected_slide_ids: list[str],
    contract: dict[str, Any],
    contract_hash: str,
    claim_ledger: dict[str, Any],
    claim_ledger_hash: str,
    required_claim_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    required_claim_ids_by_slide: dict[str, list[str]] | None = None,
    html_by_slide: dict[str, str] | None = None,
    browser_inspector=None,
    overflow_autofit: dict[str, Any] | None = None,
    canonical_validation: dict[str, Any] | None = None,
    generation_attempt_evidence_hashes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    validate_claim_ledger(claim_ledger)
    structure = inspect_contract(html_text, expected_slide_ids, contract, contract_hash)
    structural_signatures = (canonical_validation or {}).get("structural_signatures") or {}
    if structural_signatures.get("applicable"):
        checked = int(structural_signatures.get("checked_slide_count") or 0)
        matched = int(structural_signatures.get("matched_slide_count") or 0)
        structure = {
            **structure,
            "structural_signature_checked_count": checked,
            "structural_signature_matched_count": matched,
            "structural_signature_percent": round(matched * 100 / checked, 2) if checked else 0,
        }
    claims = audit_html_claims(html_text, claim_ledger, required_claim_ids=required_claim_ids)
    page_claims = None
    if required_claim_ids_by_slide is not None:
        if html_by_slide is None:
            raise ValidationError("逐页 required claim 门禁缺少页面片段")
        if list(required_claim_ids_by_slide) != expected_slide_ids or list(html_by_slide) != expected_slide_ids:
            raise ValidationError("逐页 required claim 门禁顺序与预期页面不一致")
        page_claims = audit_html_claims_by_slide(html_by_slide, claim_ledger, required_claim_ids_by_slide)
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
        *[{
            "source": "claim_ledger", "code": "missing_required_claim", "evidence": item["value"],
            **({"slide_id": item["slide_id"]} if item.get("slide_id") else {}),
        } for item in (page_claims or claims)["missing_required"]],
        *([] if canonical_validation is None or canonical_validation.get("passed") else [{
            "source": "canonical_validator",
            "code": "canonical_validation_failed",
            "evidence": "; ".join(canonical_validation.get("errors", [])[:5]) or "canonical validator 未通过",
        }]),
        *[{"source": "chromium", **item} for item in browser_blockers],
    ]
    overflow = [item for item in browser_blockers if item.get("code") in _OVERFLOW_CODES]
    autofit = None if overflow_autofit is None else dict(overflow_autofit)
    if autofit is not None:
        # The final post-render Chromium inspection is the authoritative
        # terminal state.  The bounded autofit loop can apply its last rule
        # at the round limit and return before its bookkeeping observes the
        # now-green document.  Promote that exact green state only when no
        # deterministic target remains; a real residual overflow always
        # stays fail-closed.
        terminal_geometry_green = (
            isinstance(browser, dict)
            and bool(browser.get("available"))
            and bool(browser.get("passed"))
            and not browser_blockers
        )
        if terminal_geometry_green and autofit.get("remaining") == []:
            autofit["converged"] = True
        elif overflow:
            autofit["converged"] = False
    evidence = {
        "passed": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers,
        "presentation_technical_contract_hash": contract_hash,
        # Compatibility alias for persisted records created before the
        # PresentationTechnicalContract split.
        "design_contract_hash": contract_hash,
        "claim_ledger_hash": claim_ledger_hash,
        "rendered_html_hash": hashlib.sha256(html_text.encode()).hexdigest(),
        "layout": structure,
        "claims": {
            "binding_count": claims["binding_count"],
            "unbound_count": claims["unbound_count"],
            "unbound": claims["unbound"],
            "required_count": (page_claims or claims)["required_count"],
            "covered_required_count": (page_claims or claims)["covered_required_count"],
            "covered_required_claim_ids": claims["covered_required_claim_ids"],
            "missing_required_count": (page_claims or claims)["missing_required_count"],
            "missing_required": (page_claims or claims)["missing_required"],
            "required_claim_ids_by_slide": required_claim_ids_by_slide,
            "page_coverage": None if page_claims is None else {
                slide_id: {
                    "required_count": page["required_count"],
                    "covered_required_count": page["covered_required_count"],
                    "covered_required_claim_ids": page["covered_required_claim_ids"],
                    "missing_required_count": page["missing_required_count"],
                    "missing_required": page["missing_required"],
                    "text_hash": page["text_hash"],
                }
                for slide_id, page in page_claims["pages"].items()
            },
            "text_hash": claims["text_hash"],
        },
        "canonical_validator": canonical_validation,
        "generation_attempt_evidence_hashes": list(generation_attempt_evidence_hashes or ()),
        "geometry": {
            "available": browser is not None and bool(browser.get("available")),
            "passed": None if browser is None else bool(browser.get("passed")) and not browser_blockers,
            "overflow_count": len(overflow),
            "blocker_count": len(browser_blockers),
            "engine": None if browser is None else browser.get("engine"),
            "engine_version": None if browser is None else browser.get("engine_version"),
            "viewport": None if browser is None else browser.get("viewport"),
        },
        "overflow_autofit": autofit,
    }
    return seal_post_render_evidence(evidence)


def enforce_post_render_gate(*args, **kwargs) -> dict[str, Any]:
    evidence = run_post_render_gate(*args, **kwargs)
    if evidence["blockers"]:
        summary = "；".join(f"{item.get('code')}:{item.get('evidence', '')}" for item in evidence["blockers"][:5])
        raise ValidationError(f"渲染后硬门禁未通过：{summary}")
    return evidence
