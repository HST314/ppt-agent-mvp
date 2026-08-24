from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from ..generation.errors import ErrorContext, RenderValidationError
from .assets import ResolvedAsset


UNSAFE_TAGS = frozenset({"script", "iframe", "object", "embed", "form", "base", "link", "video", "audio"})
URL_ATTRIBUTES = frozenset({"src", "href", "poster", "action"})
REMOTE_CSS = re.compile(r"(?:url\s*\(\s*['\"]?\s*(?:https?:)?//|@import\b)", re.I)
CSS_URL = re.compile(r"url\s*\(\s*(?:['\"])?([^)'\"]+)(?:['\"])?\s*\)", re.I)


@dataclass(frozen=True)
class ValidationReport:
    passed: bool
    issues: tuple[dict[str, Any], ...]
    expected_slide_ids: tuple[str, ...]
    observed_slide_ids: tuple[str, ...]
    asset_paths: tuple[str, ...]
    browser: dict[str, Any] | None
    evidence_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": list(self.issues),
            "expected_slide_ids": list(self.expected_slide_ids),
            "observed_slide_ids": list(self.observed_slide_ids),
            "asset_paths": list(self.asset_paths),
            "browser": self.browser,
            "evidence_hash": self.evidence_hash,
        }


class _DeckParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.issues: list[dict[str, Any]] = []
        self.slide_ids: list[str] = []
        self.asset_paths: list[str] = []
        self.slide_depth = 0
        self.current_slide: str | None = None
        self.text_counts: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {name.lower(): value or "" for name, value in attrs}
        if tag in UNSAFE_TAGS:
            self.issues.append(_issue("unsafe_tag", f"禁止标签：{tag}"))
        if any(name.startswith("on") for name in attributes):
            self.issues.append(_issue("event_handler", "HTML 包含事件处理属性"))
        if "style" in attributes and REMOTE_CSS.search(attributes["style"]):
            self.issues.append(_issue("remote_css", "内联样式包含远程依赖"))
        for name in URL_ATTRIBUTES & set(attributes):
            value = attributes[name].strip()
            if not value:
                continue
            parsed = urlparse(value)
            if parsed.scheme in {"http", "https", "javascript"} or value.startswith("//"):
                self.issues.append(_issue("external_url", "HTML 包含外部或可执行 URL"))
            elif name == "src" and tag == "img":
                self.asset_paths.append(value)
        classes = set(attributes.get("class", "").split())
        if tag == "section" and "slide" in classes:
            if self.slide_depth:
                self.issues.append(_issue("nested_slide", "页面节点不可嵌套"))
            slide_id = attributes.get("id")
            data_slide_id = attributes.get("data-slide-id")
            if not slide_id or slide_id != data_slide_id:
                self.issues.append(_issue("slide_identity", "页面 ID 与 data-slide-id 必须一致"))
            else:
                self.slide_ids.append(slide_id)
                self.current_slide = slide_id
                self.text_counts.setdefault(slide_id, 0)
            self.slide_depth += 1
        elif tag == "section" and self.slide_depth:
            self.slide_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "section" and self.slide_depth:
            self.slide_depth -= 1
            if self.slide_depth == 0:
                self.current_slide = None

    def handle_data(self, data: str) -> None:
        if self.current_slide:
            self.text_counts[self.current_slide] += len(re.sub(r"\s+", "", data))


class TechnicalValidator:
    """Synchronous DOM, resource, safety, canvas and optional geometry gate."""

    def __init__(self, browser_inspector=None, *, require_browser: bool = False):
        self.browser_inspector = browser_inspector
        self.require_browser = require_browser

    def validate(self, html_text: str, expected_slide_ids: Iterable[str], assets: Mapping[str, ResolvedAsset] | None = None) -> ValidationReport:
        expected = tuple(expected_slide_ids)
        assets = assets or {}
        parser = _DeckParser()
        parser.feed(html_text)
        parser.close()
        issues = list(parser.issues)
        observed = tuple(parser.slide_ids)
        if observed != expected:
            issues.append(_issue("slide_set", "页面集合或顺序与 DeckSpec 不一致", expected=list(expected), observed=list(observed)))
        if len(set(observed)) != len(observed):
            issues.append(_issue("duplicate_slide", "页面 ID 重复"))
        expected_paths = {asset.offline_path for asset in assets.values()}
        style = _extract_style(html_text)
        observed_paths = set(parser.asset_paths)
        observed_paths.update(
            value.strip()
            for value in CSS_URL.findall(style)
            if value.strip() and not value.strip().startswith(("data:", "http://", "https://", "//"))
        )
        if observed_paths != expected_paths:
            issues.append(_issue("asset_closure", "HTML 资源引用与资源清单不一致", expected=sorted(expected_paths), observed=sorted(observed_paths)))
        for slide_id, count in parser.text_counts.items():
            if count > 2_400:
                issues.append(_issue("content_budget", "页面文本预算超限", slide_id=slide_id, characters=count))
        if "width:1280px" not in style or "height:720px" not in style or "overflow:hidden" not in style:
            issues.append(_issue("canvas_contract", "renderer 未固定 1280×720 画布与溢出边界"))
        if REMOTE_CSS.search(style):
            issues.append(_issue("remote_css", "样式包含远程依赖"))
        browser = None
        if self.browser_inspector is not None:
            browser = self.browser_inspector.inspect(html_text, list(expected))
            for item in browser.get("issues", []):
                if item.get("severity", "blocker") == "blocker":
                    issues.append({key: value for key, value in {
                        "code": item.get("code", "browser_gate"),
                        "message": item.get("message", "浏览器技术检查失败"),
                        "slide_id": item.get("slide_id", ""),
                        "element_id": item.get("element_id", ""),
                        "geometry": item.get("geometry"),
                        "source": "browser",
                    }.items() if value not in (None, "")})
            if not browser.get("available", False) or not browser.get("passed", False):
                if not any(item.get("code") in {"render_unavailable", "browser_gate"} for item in issues):
                    issues.append(_issue("browser_gate", "浏览器技术检查未通过"))
        elif self.require_browser:
            issues.append(_issue("browser_required", "未配置必需的 Chromium 技术检查"))
        evidence = {
            "expected": expected,
            "observed": observed,
            "assets": sorted(observed_paths),
            "issues": issues,
            "browser": _safe_browser_evidence(browser),
        }
        evidence_hash = hashlib.sha256(repr(evidence).encode()).hexdigest()
        report = ValidationReport(not issues, tuple(issues), expected, observed, tuple(sorted(observed_paths)), browser, evidence_hash)
        if not report.passed:
            diagnostics = tuple({key: item[key] for key in ("code", "slide_id", "element_id", "geometry", "source") if key in item} for item in issues)
            raise RenderValidationError(
                "确定性 renderer 技术门禁未通过",
                context=ErrorContext(stage="render", field_path=issues[0].get("code") if issues else None),
                diagnostics=diagnostics,
            )
        return report

    def readiness(self) -> dict[str, Any]:
        if self.browser_inspector is None:
            return {"ready": not self.require_browser, "browser_required": self.require_browser, "engine": None}
        probe = "<!doctype html><html><style>.slide{width:1280px;height:720px;overflow:hidden}</style><section class=\"slide\" id=\"slide-001\" data-slide-id=\"slide-001\"><h1>ready</h1></section></html>"
        result = self.browser_inspector.inspect(probe, ["slide-001"])
        return {"ready": bool(result.get("available") and result.get("passed")), "browser_required": self.require_browser, "engine": result.get("engine"), "engine_version": result.get("engine_version"), "viewport": result.get("viewport"), "issues": [{"code": item.get("code"), "severity": item.get("severity")} for item in result.get("issues", [])]}


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _extract_style(html_text: str) -> str:
    match = re.search(r"<style\b[^>]*>(.*?)</style\s*>", html_text, re.I | re.S)
    return re.sub(r"\s+", "", match.group(1)) if match else ""


def _safe_browser_evidence(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: value.get(key) for key in ("available", "passed", "engine", "engine_version", "viewport")}
