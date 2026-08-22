from __future__ import annotations

import hashlib
import math
import re
from html.parser import HTMLParser
from typing import Any


_CARD_CLASS = re.compile(r"(?:^|-)(?:card|tile|stat|step|rowline|panel|item)(?:-|$)", re.I)
_HARD_BROWSER_CODES = {
    "content_out_of_bounds", "slide_scroll_overflow", "element_scroll_overflow",
    "render_unavailable", "invalid_measurement", "empty_slide", "broken_image",
    "missing_title", "title_too_small", "text_too_small", "undefined_layout_class",
}


def layout_capacity_policy(contract: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Return the server-owned readable-capacity budget for each locked page."""
    profiles = {
        "cover": {"max_cards": 4, "max_list_items": 8, "max_visible_characters": 620, "max_estimated_lines": 18, "minimum_body_font_px": 16},
        "body": {"max_cards": 8, "max_list_items": 16, "max_visible_characters": 1000, "max_estimated_lines": 32, "minimum_body_font_px": 16},
        "closing": {"max_cards": 6, "max_list_items": 12, "max_visible_characters": 720, "max_estimated_lines": 24, "minimum_body_font_px": 16},
    }
    return {
        item["slide_id"]: {
            **profiles.get(item.get("visual_role"), profiles["body"]),
            "layout_id": item["layout_id"],
        }
        for item in contract.get("slide_contracts", [])
    }


class _CapacityParser(HTMLParser):
    _SKIP = {"head", "style", "script", "template", "noscript", "title"}
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, str | None]] = []
        self.pages: dict[str, dict[str, Any]] = {}
        self.active_slide: str | None = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {str(name).lower(): value or "" for name, value in attrs}
        inherited_hidden = self.stack[-1][1] if self.stack else False
        hidden = inherited_hidden or tag in self._SKIP or "hidden" in values or values.get("aria-hidden", "").lower() == "true"
        slide_id = None
        if tag == "section" and "slide" in values.get("class", "").split() and self.active_slide is None:
            slide_id = values.get("data-slide-id") or values.get("id") or ""
            self.active_slide = slide_id
            self.pages.setdefault(slide_id, {"visible_characters": 0, "estimated_lines": 0, "list_items": 0, "cards": 0, "text_hash_parts": []})
        if self.active_slide and not hidden:
            page = self.pages[self.active_slide]
            if tag == "li":
                page["list_items"] += 1
            classes = values.get("class", "").split()
            if any(_CARD_CLASS.search(name) for name in classes):
                page["cards"] += 1
        if tag not in self._VOID:
            self.stack.append((tag, hidden, slide_id))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        target = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            stack_tag, _, slide_id = self.stack[index]
            if stack_tag != target:
                continue
            del self.stack[index:]
            if slide_id is not None:
                self.active_slide = next((item[2] for item in reversed(self.stack) if item[2] is not None), None)
            return

    def handle_data(self, data):
        if not self.active_slide or not self.stack or self.stack[-1][1]:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        page = self.pages[self.active_slide]
        compact = re.sub(r"\s+", "", text)
        page["visible_characters"] += len(compact)
        page["estimated_lines"] += max(1, math.ceil(len(compact) / 42))
        page["text_hash_parts"].append(text)


def inspect_layout_capacity(html_text: str, contract: dict[str, Any]) -> dict[str, Any]:
    """Check coarse content capacity before expensive final publication gates.

    This is intentionally conservative.  Chromium geometry remains
    authoritative; the static budget catches obviously overloaded pages early
    and gives a builder a useful regeneration payload instead of inviting an
    unsafe whole-page scale-down.
    """
    policy = layout_capacity_policy(contract)
    parser = _CapacityParser()
    parser.feed(html_text)
    parser.close()
    pages: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    for slide_id, budget in policy.items():
        measured = parser.pages.get(slide_id, {"visible_characters": 0, "estimated_lines": 0, "list_items": 0, "cards": 0, "text_hash_parts": []})
        public = {key: measured[key] for key in ("visible_characters", "estimated_lines", "list_items", "cards")}
        public["visible_text_hash"] = hashlib.sha256("\n".join(measured["text_hash_parts"]).encode()).hexdigest()
        pages[slide_id] = {"budget": budget, "measured": public}
        for metric, limit_key in (
            ("cards", "max_cards"),
            ("list_items", "max_list_items"),
            ("visible_characters", "max_visible_characters"),
            ("estimated_lines", "max_estimated_lines"),
        ):
            if public[metric] > budget[limit_key]:
                issues.append({
                    "code": "layout_capacity_exceeded",
                    "slide_id": slide_id,
                    "layout_id": budget["layout_id"],
                    "metric": metric,
                    "actual": public[metric],
                    "maximum": budget[limit_key],
                    "suggestion": "压缩正文、减少卡片/行项目，或切换高容量布局/拆页；不得隐藏内容或突破字号下限",
                })
    return {"passed": not issues, "issues": issues, "pages": pages, "policy": policy}


def structured_canonical_blockers(validation: dict[str, Any], contract: dict[str, Any]) -> list[dict[str, Any]]:
    slides = contract.get("slide_contracts", [])
    blockers = []
    for message in validation.get("errors", []) if isinstance(validation, dict) else []:
        index_match = re.match(r"Slide\s+(\d+):\s*(.*)", str(message))
        index = int(index_match.group(1)) - 1 if index_match else -1
        detail = index_match.group(2) if index_match else str(message)
        slide_id = slides[index]["slide_id"] if 0 <= index < len(slides) else ""
        if "text-align:center" in detail:
            rule_id, selector, violation, expected = "swiss-body-title-left", "top-title-area", "text-align:center", "text-align:left"
        elif "align-self" in detail:
            rule_id, selector, violation, expected = "swiss-body-title-top", "top-heading", "align-self:center", "align-self:flex-start"
        else:
            rule_id, selector, violation, expected = "canonical-validator", "slide", detail, "conform to locked canonical validator"
        blockers.append({
            "rule_id": rule_id,
            "slide_id": slide_id,
            "selector": selector,
            "violation": violation,
            "expected": expected,
            "message": str(message),
        })
    return blockers


def hard_browser_blockers(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(evidence, dict):
        return [{"code": "invalid_measurement", "evidence": "浏览器预检未返回对象"}]
    issues = evidence.get("issues") if isinstance(evidence.get("issues"), list) else []
    blockers = [item for item in issues if item.get("severity") == "blocker" or item.get("code") in _HARD_BROWSER_CODES]
    if not evidence.get("available") and not any(item.get("code") == "render_unavailable" for item in blockers):
        blockers.append({"code": "render_unavailable", "evidence": "Chromium 预检不可用"})
    if evidence.get("available") and not evidence.get("passed") and not blockers:
        blockers.append({"code": "invalid_measurement", "evidence": "Chromium 预检失败但没有结构化问题"})
    return blockers
