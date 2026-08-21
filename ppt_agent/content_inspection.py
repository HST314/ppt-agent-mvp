from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any


_SKIPPED_TAGS = frozenset({"head", "script", "style", "template", "noscript", "title"})
_TEXT_REGION_TAGS = frozenset({
    "section", "article", "aside", "header", "footer", "main", "nav", "div",
    "p", "blockquote", "figcaption", "li", "dt", "dd", "td", "th", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
})
_DEFAULT_FIELD_LABELS = frozenset({
    "汇报日期",
    "汇报部门",
    "汇报单位",
    "演示日期",
    "汇报日期·汇报部门",
    "汇报日期|汇报部门",
    "汇报日期/汇报部门",
    "日期·部门",
    "日期|部门",
    "日期/部门",
})
_PLACEHOLDER_PATTERNS = (
    ("placeholder_token", re.compile(r"(?<![A-Za-z0-9])X{2,}(?![A-Za-z0-9])", re.IGNORECASE)),
    ("placeholder_token", re.compile(r"(?<![A-Za-z0-9])(?:TBD|TBC|TODO)(?![A-Za-z0-9])", re.IGNORECASE)),
    ("template_marker", re.compile(
        r"(?:\{\{[^{}]{1,80}\}\}|\$\{[^{}]{1,80}\}|<<[^<>]{1,80}>>|20XX年|"
        r"[\[【]\s*(?:必填|待填|待补|placeholder)\s*[\]】]|"
        r"(?:(?:19|20)\d{2}|[XＸ]{1,4})\s*年\s*[XＸ]{1,2}\s*月(?:\s*[XＸ]{1,2}\s*日)?|"
        r"[XＸ]{1,2}\s*月\s*[XＸ]{1,2}\s*日)",
        re.IGNORECASE,
    )),
)
_DISCLOSED_MISSING_RE = re.compile(r"(?:数据)?待(?:补充|确认|核实|定)|暂无数据|尚未提供")
_DATE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*(?:[-/.年])\s*\d{1,2}\s*(?:[-/.月])\s*\d{1,2}\s*日?(?!\d)")
_METRIC_RE = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:[\s,，]\d{3})*(?:\.\d+)?\s*"
    r"(?:%|％|亿元|万元|百万\+?|亿\+?|万\+?|美元|人民币|元|个\s*工作日|工作日|"
    r"毫秒|秒|分钟|小时|天|周|个月|月|年|倍|[×xX]|条|次|人|家|项)(?![A-Za-z])"
)
_CRITICAL_FACT_CONTEXT = re.compile(
    r"预算|成本|金额|收入|利润|增长|提升|下降|转化|覆盖|日均|月均|年均|响应|解决率|"
    r"满意|完成率|准确|用户|客户|业务线|日期|周期|时长|同比|环比|KPI|SLA|服务等级|"
    r"可用性|节省|试点|扩容|承诺|保证|确保|目标|预期|预计|工作日|会后|截止|输出|交付|里程碑",
    re.IGNORECASE,
)


class _VisibleTextParser(HTMLParser):
    """Collect visible text with the nearest slide and editable element IDs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[dict[str, Any]] = []
        self.chunks: list[dict[str, str]] = []
        self._region_counter = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        parent = self._stack[-1] if self._stack else {
            "hidden": False,
            "slide_id": "",
            "element_id": "",
            "region_id": "",
        }
        style = attributes.get("style", "")
        hidden = bool(
            parent["hidden"]
            or tag.lower() in _SKIPPED_TAGS
            or "hidden" in attributes
            or attributes.get("aria-hidden", "").lower() == "true"
            or re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style, re.IGNORECASE)
        )
        slide_id = parent["slide_id"]
        classes = attributes.get("class", "").split()
        if tag.lower() == "section" and "slide" in classes:
            slide_id = attributes.get("data-slide-id") or attributes.get("id") or slide_id
        element_id = attributes.get("data-element-id") or parent["element_id"]
        region_id = parent["region_id"]
        if tag.lower() in _TEXT_REGION_TAGS:
            self._region_counter += 1
            region_id = f"region-{self._region_counter}"
        self._stack.append({
            "tag": tag.lower(),
            "hidden": hidden,
            "slide_id": slide_id,
            "element_id": element_id,
            "region_id": region_id,
        })

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        target = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].get("tag") == target:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not self._stack:
            return
        current = self._stack[-1]
        text = re.sub(r"\s+", " ", data).strip()
        if text and not current["hidden"] and current["slide_id"]:
            self.chunks.append({
                "text": text,
                "slide_id": current["slide_id"],
                "element_id": current["element_id"],
                "region_id": current["region_id"],
            })


def _binding_text(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return re.sub(r"[\s,，]", "", text).replace("％", "%").casefold()


def _snippet(text: str, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _locally_disclosed(text: str, start: int, end: int, radius: int = 24) -> bool:
    return bool(_DISCLOSED_MISSING_RE.search(text[max(0, start - radius): min(len(text), end + radius)]))


def _issue(
    *,
    code: str,
    severity: str,
    message: str,
    evidence: str,
    suggestion: str,
    slide_id: str,
    element_id: str,
) -> dict[str, str]:
    identity = json.dumps(
        {"code": code, "slide_id": slide_id, "element_id": element_id, "evidence": evidence},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "issue_id": f"content-{hashlib.sha256(identity.encode()).hexdigest()[:20]}",
        "severity": severity,
        "level": "element" if element_id else "slide",
        "code": code,
        "message": message,
        "slide_id": slide_id,
        "element_id": element_id,
        "evidence": evidence,
        "suggestion": suggestion,
        "source": "semantic_deterministic",
    }


def _group_visible_chunks(chunks: list[dict[str, str]]) -> list[dict[str, str]]:
    """Join adjacent visible text by editable element or slide.

    Inline markup commonly splits a fact (for example ``SLA <strong>99.5%</strong>``)
    across parser callbacks. Grouping preserves the surrounding claim context
    without mixing separately addressable elements.
    """

    grouped: dict[tuple[str, str, str], list[str]] = {}
    for chunk in chunks:
        key = (chunk["slide_id"], chunk["element_id"], chunk.get("region_id", ""))
        grouped.setdefault(key, []).append(chunk["text"])
    return [
        {"slide_id": slide_id, "element_id": element_id, "text": " ".join(parts)}
        for (slide_id, element_id, _), parts in grouped.items()
    ]


def _inspect_chunks(chunks: list[dict[str, str]], source_binding: Any) -> tuple[list[dict[str, str]], str]:
    bound = _binding_text(source_binding)
    issues: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(item: dict[str, str]) -> None:
        identity = (item["code"], item["slide_id"], item["element_id"], item["evidence"])
        if identity not in seen:
            seen.add(identity)
            issues.append(item)

    for chunk in _group_visible_chunks(chunks):
        text = chunk["text"]
        compact = re.sub(r"\s+", "", text).replace("｜", "|")
        location = {"slide_id": chunk["slide_id"], "element_id": chunk["element_id"]}
        if compact in _DEFAULT_FIELD_LABELS:
            add(_issue(
                code="unbound_default_field",
                severity="blocker",
                message="页面仍包含未填写的默认汇报字段",
                evidence=f"可见文本：{_snippet(text)}；冻结任务资料中没有可发布的字段值",
                suggestion="填写经确认的日期/部门，或删除该默认字段",
                **location,
            ))

        for code, pattern in _PLACEHOLDER_PATTERNS:
            for match in pattern.finditer(text):
                add(_issue(
                    code=code,
                    severity="blocker",
                    message="页面仍包含显式占位内容",
                    evidence=f"可见文本“{_snippet(text)}”命中占位符“{match.group(0)}”",
                    suggestion="使用冻结资料中的已确认事实替换；没有来源时删除该指标或明确标注数据待确认",
                    **location,
                ))

        disclosed_missing = list(_DISCLOSED_MISSING_RE.finditer(text))
        for match in disclosed_missing:
            add(_issue(
                code="unconfirmed_fact",
                severity="warning",
                message="页面包含已披露但尚未确认的事实",
                evidence=f"可见文本“{_snippet(text)}”包含“{match.group(0)}”",
                suggestion="交付前补充来源并确认；若暂时无法确认，请保留显式披露并由用户处置",
                **location,
            ))

        occupied: list[tuple[int, int]] = []
        for match in _DATE_RE.finditer(text):
            occupied.append(match.span())
            if _locally_disclosed(text, *match.span()):
                continue
            token = _binding_text(match.group(0))
            if token not in bound:
                add(_issue(
                    code="unverified_critical_fact",
                    severity="blocker",
                    message="页面日期未绑定到冻结任务资料",
                    evidence=f"可见日期“{match.group(0)}”未在冻结输入中找到对应值",
                    suggestion="确认日期来源并写入任务资料，或删除该日期",
                    **location,
                ))

        for match in _METRIC_RE.finditer(text):
            if any(start <= match.start() and match.end() <= end for start, end in occupied):
                continue
            if _locally_disclosed(text, *match.span()):
                continue
            token = _binding_text(match.group(0))
            if token in bound:
                continue
            severity = "blocker" if _CRITICAL_FACT_CONTEXT.search(text) else "warning"
            add(_issue(
                code="unverified_critical_fact" if severity == "blocker" else "unverified_fact",
                severity=severity,
                message="页面事实未绑定到冻结任务资料",
                evidence=f"可见事实“{match.group(0)}”未在冻结输入中找到对应值；上下文：{_snippet(text)}",
                suggestion="补充来源绑定并确认该值；若只是结构性说明，请改写为不带未经确认数值的表达",
                **location,
            ))

    return issues, bound


def inspect_content_quality(html_text: str, source_binding: Any) -> dict[str, Any]:
    """Return deterministic semantic issues from visible slide text.

    This is intentionally separate from HTML safety validation. It detects
    explicit placeholders and high-signal facts that are absent from the frozen
    user input, while ignoring scripts, styles, templates and hidden nodes.
    """

    parser = _VisibleTextParser()
    parser.feed(html_text)
    parser.close()
    issues, bound = _inspect_chunks(parser.chunks, source_binding)
    visible_fingerprint = hashlib.sha256(
        json.dumps(parser.chunks, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "available": True,
        "passed": not issues,
        "issues": issues,
        "visible_text_hash": visible_fingerprint,
        "source_binding_hash": hashlib.sha256(bound.encode()).hexdigest(),
    }
