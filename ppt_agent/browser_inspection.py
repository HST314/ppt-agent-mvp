from __future__ import annotations

import hashlib
import time
from typing import Any


VIEWPORT = {"width": 1280, "height": 720}
TOLERANCE_PX = 1.0
MIN_TITLE_PX = 32.0
MIN_BODY_PX = 16.0
MIN_META_PX = 12.0


def _issue(code: str, message: str, *, severity: str = "blocker", slide_id: str = "", element_id: str = "", evidence: str, suggestion: str) -> dict:
    stable = hashlib.sha256(f"{code}\0{slide_id}\0{element_id}\0{evidence}".encode()).hexdigest()[:12]
    return {
        "issue_id": f"browser-{code}-{stable}",
        "severity": severity,
        "level": "element" if element_id else "slide" if slide_id else "deck",
        "code": code,
        "message": message,
        "slide_id": slide_id,
        "element_id": element_id,
        "evidence": evidence,
        "suggestion": suggestion,
    }


class ChromiumDeckInspector:
    """Fail-closed DOM geometry checks using the production browser engine."""

    enforce_on_generation = True

    def __init__(self, *, timeout_ms: int = 15_000):
        self.timeout_ms = timeout_ms

    def inspect(self, html_text: str, expected_slide_ids: list[str]) -> dict:
        started = time.monotonic()
        try:
            raw = self._measure(html_text)
        except Exception:
            issue = _issue(
                "render_unavailable",
                "Chromium 渲染检查不可用，不能判定检查通过",
                evidence="未产生可信的 Chromium DOM 几何测量",
                suggestion="安装锁定 Playwright/Chromium 并重新执行检查",
            )
            return {
                "available": False,
                "passed": False,
                "engine": "chromium",
                "engine_version": None,
                "viewport": dict(VIEWPORT),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                "issues": [issue],
                "slides": [],
            }

        issues = self._issues(raw, expected_slide_ids)
        return {
            "available": True,
            "passed": not issues,
            "engine": "chromium",
            "engine_version": raw["engine_version"],
            "viewport": dict(VIEWPORT),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            "issues": issues,
            "slides": raw["slides"],
        }

    def _measure(self, html_text: str) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport=VIEWPORT, reduced_motion="reduce")
                try:
                    page = context.new_page()
                    page.set_default_timeout(self.timeout_ms)
                    page.route("http://**/*", lambda route: route.abort())
                    page.route("https://**/*", lambda route: route.abort())
                    page.set_content(html_text, wait_until="load", timeout=self.timeout_ms)
                    page.add_style_tag(content="*,*::before,*::after{animation:none!important;transition:none!important}")
                    page.evaluate("document.fonts && document.fonts.ready")
                    slides = page.eval_on_selector_all(
                        ".slide",
                        """slides => slides.map((slide, slideIndex) => {
                            const rect = slide.getBoundingClientRect();
                            const visible = element => {
                                const style = getComputedStyle(element);
                                const box = element.getBoundingClientRect();
                                return style.display !== 'none' && style.visibility !== 'hidden' &&
                                    Number(style.opacity) !== 0 && box.width > 0 && box.height > 0;
                            };
                            const label = (element, index) =>
                                element.getAttribute('data-element-id') || element.id ||
                                `${element.tagName.toLowerCase()}-${index + 1}`;
                            const descendants = [...slide.querySelectorAll('*')].filter(visible);
                            const overflowRoots = new Map();
                            const overflowSuppressed = [];
                            const scrolling = [];
                            // Root-cause dedup: when an ancestor already exceeds the slide
                            // bounds, descendant flags share that root cause.  Keep the
                            // outermost element and fold children into its evidence.
                            descendants.forEach((element, index) => {
                                const box = element.getBoundingClientRect();
                                const elementId = label(element, index);
                                const delta = {
                                    left: rect.left - box.left,
                                    top: rect.top - box.top,
                                    right: box.right - rect.right,
                                    bottom: box.bottom - rect.bottom,
                                };
                                if (Math.max(delta.left, delta.top, delta.right, delta.bottom) > 1) {
                                    let ancestor = element.parentElement;
                                    let root = null;
                                    while (ancestor && ancestor !== slide) {
                                        if (overflowRoots.has(ancestor)) { root = overflowRoots.get(ancestor); break; }
                                        ancestor = ancestor.parentElement;
                                    }
                                    if (root) root.suppressed.push(elementId);
                                    else overflowRoots.set(element, {element_id: elementId, tag: element.tagName.toLowerCase(), suppressed: [], ...delta});
                                }
                            });
                            const overflows = [...overflowRoots.values()];
                            descendants.forEach((element, index) => {
                                if (element.clientWidth > 0 && element.clientHeight > 0 &&
                                    (element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1)) {
                                    let coveredBy = '';
                                    let node = element;
                                    while (node && node !== slide) {
                                        if (overflowRoots.has(node)) { coveredBy = overflowRoots.get(node).element_id; break; }
                                        node = node.parentElement;
                                    }
                                    scrolling.push({
                                        element_id: label(element, index),
                                        tag: element.tagName.toLowerCase(),
                                        client_width: element.clientWidth,
                                        client_height: element.clientHeight,
                                        scroll_width: element.scrollWidth,
                                        scroll_height: element.scrollHeight,
                                        covered_by: coveredBy,
                                    });
                                }
                            });
                            const titleElement = slide.querySelector('[data-element-id="title"],h1,h2,h3');
                            const title = titleElement && visible(titleElement) ? {
                                element_id: label(titleElement, descendants.indexOf(titleElement)),
                                font_size: Number.parseFloat(getComputedStyle(titleElement).fontSize),
                                text: (titleElement.innerText || '').trim().slice(0, 160),
                            } : null;
                            // Role-based thresholds: body/list text stays >=16px; kickers,
                            // footers, page numbers and similar chrome get their own floor.
                            const metaPattern = /kicker|eyebrow|caption|foot|note|page|index|number|chrome|badge|label|source|meta|tag/i;
                            const smallTextRoots = new Set();
                            const smallText = [...slide.querySelectorAll('p,li,td,th,small,figcaption,[data-element-id]')]
                                .filter(element => visible(element) && (element.innerText || '').trim())
                                .map((element, index) => {
                                    const role = (element.matches('small,figcaption') ||
                                        metaPattern.test(element.getAttribute('data-element-id') || '') ||
                                        metaPattern.test(element.className || '')) ? 'meta' : 'body';
                                    return {
                                        element,
                                        element_id: label(element, descendants.indexOf(element) >= 0 ? descendants.indexOf(element) : index),
                                        font_size: Number.parseFloat(getComputedStyle(element).fontSize),
                                        text: (element.innerText || '').trim().slice(0, 120),
                                        role,
                                        minimum: role === 'meta' ? 12 : 16,
                                    };
                                })
                                .filter(item => {
                                    if (item.font_size >= item.minimum) return false;
                                    let ancestor = item.element.parentElement;
                                    while (ancestor && ancestor !== slide) {
                                        if (smallTextRoots.has(ancestor)) return false;
                                        ancestor = ancestor.parentElement;
                                    }
                                    smallTextRoots.add(item.element);
                                    return true;
                                })
                                .map(({element, ...rest}) => rest)
                                .slice(0, 8);
                            const brokenImages = [...slide.querySelectorAll('img')]
                                .filter(image => !image.complete || image.naturalWidth === 0)
                                .map((image, index) => label(image, descendants.indexOf(image) >= 0 ? descendants.indexOf(image) : index));
                            return {
                                slide_id: slide.getAttribute('data-slide-id') || slide.id || '',
                                index: slideIndex,
                                width: rect.width,
                                height: rect.height,
                                text_length: (slide.innerText || '').trim().length,
                                visual_count: slide.querySelectorAll('img,svg,canvas,table').length,
                                title,
                                overflows: overflows.slice(0, 12),
                                scrolling: scrolling.slice(0, 12),
                                small_text: smallText,
                                broken_images: brokenImages.slice(0, 8),
                            };
                        })""",
                    )
                    return {"engine_version": browser.version, "slides": slides}
                finally:
                    context.close()
            finally:
                browser.close()

    @staticmethod
    def _issues(raw: dict, expected_slide_ids: list[str]) -> list[dict]:
        issues: list[dict] = []
        slides = raw.get("slides") if isinstance(raw, dict) else None
        if not isinstance(slides, list):
            return [_issue(
                "invalid_measurement",
                "Chromium 渲染测量结果无效",
                evidence="slides 测量不是数组",
                suggestion="检查浏览器检查器版本并重新执行",
            )]

        actual_ids = [slide.get("slide_id") for slide in slides if isinstance(slide, dict)]
        if actual_ids != list(expected_slide_ids):
            issues.append(_issue(
                "slide_sequence_mismatch",
                "浏览器中的页面数量或顺序与候选稿不一致",
                evidence=f"expected={expected_slide_ids}; actual={actual_ids}",
                suggestion="重新生成缺失、重复或乱序的页面",
            ))

        expected = set(expected_slide_ids)
        for slide in slides:
            if not isinstance(slide, dict):
                continue
            slide_id = str(slide.get("slide_id") or "")
            if slide_id not in expected:
                continue
            width, height = float(slide.get("width") or 0), float(slide.get("height") or 0)
            if abs(width - VIEWPORT["width"]) > TOLERANCE_PX or abs(height - VIEWPORT["height"]) > TOLERANCE_PX:
                issues.append(_issue(
                    "canvas_size",
                    "页面没有按 1280×720 画布渲染",
                    slide_id=slide_id,
                    evidence=f"rendered={width:.1f}x{height:.1f}px; expected=1280x720px",
                    suggestion="使用锁定模板的固定画布，不要覆盖 slide 宽高",
                ))
            if int(slide.get("text_length") or 0) == 0 and int(slide.get("visual_count") or 0) == 0:
                issues.append(_issue(
                    "empty_slide",
                    "页面没有可见内容",
                    slide_id=slide_id,
                    evidence="可见文本长度和视觉元素数量均为 0",
                    suggestion="补充与大纲一致的标题和核心内容",
                ))
            title = slide.get("title")
            if not isinstance(title, dict) or not title.get("text"):
                issues.append(_issue(
                    "missing_title",
                    "页面缺少可见标题",
                    severity="warning",
                    slide_id=slide_id,
                    evidence="未找到可见的 data-element-id=title 或 h1/h2/h3",
                    suggestion="为页面添加清晰的语义标题",
                ))
            elif float(title.get("font_size") or 0) < MIN_TITLE_PX:
                size = float(title.get("font_size") or 0)
                issues.append(_issue(
                    "title_too_small",
                    "页面标题投屏字号过小",
                    severity="warning",
                    slide_id=slide_id,
                    element_id=str(title.get("element_id") or "title"),
                    evidence=f"computed font-size={size:.1f}px; minimum={MIN_TITLE_PX:.0f}px",
                    suggestion="使用锁定模板标题类或将标题字号提高到至少 32px",
                ))
            covered_scroll = [item for item in slide.get("scrolling") or [] if item.get("covered_by")]
            for item in slide.get("overflows") or []:
                overflow = max(float(item.get(key) or 0) for key in ("left", "top", "right", "bottom"))
                evidence = f"DOM geometry exceeds slide by {overflow:.1f}px"
                suppressed = [str(name) for name in item.get("suppressed") or []]
                if suppressed:
                    evidence += f"；同根因受影响元素 {len(suppressed)} 个: {', '.join(suppressed[:8])}"
                linked = [entry for entry in covered_scroll if str(entry.get("covered_by")) == str(item.get("element_id") or item.get("tag") or "element")]
                if linked:
                    evidence += f"；其中 {len(linked)} 个元素伴随内部滚动溢出"
                issues.append(_issue(
                    "content_out_of_bounds",
                    "元素超出页面安全边界",
                    slide_id=slide_id,
                    element_id=str(item.get("element_id") or item.get("tag") or "element"),
                    evidence=evidence,
                    suggestion="收紧内容、间距或改用容量更合适的布局；同源子元素会随根因修复一并消除",
                ))
            for item in slide.get("scrolling") or []:
                if item.get("covered_by"):
                    continue
                issues.append(_issue(
                    "element_scroll_overflow",
                    "元素内容发生滚动溢出",
                    slide_id=slide_id,
                    element_id=str(item.get("element_id") or item.get("tag") or "element"),
                    evidence=(
                        f"client={item.get('client_width')}x{item.get('client_height')}px; "
                        f"scroll={item.get('scroll_width')}x{item.get('scroll_height')}px"
                    ),
                    suggestion="精简文字、调整布局或扩大内容容器",
                ))
            for item in slide.get("small_text") or []:
                size = float(item.get("font_size") or 0)
                minimum = float(item.get("minimum") or MIN_BODY_PX)
                role = str(item.get("role") or "body")
                issues.append(_issue(
                    "text_too_small",
                    "元信息文字投屏字号过小" if role == "meta" else "正文或辅助文字投屏字号过小",
                    severity="warning",
                    slide_id=slide_id,
                    element_id=str(item.get("element_id") or "text"),
                    evidence=f"computed font-size={size:.1f}px; role={role}; minimum={minimum:.0f}px",
                    suggestion=f"精简内容并把文字字号提高到至少 {minimum:.0f}px" if role != "meta" else f"弱化展示或提高到至少 {minimum:.0f}px，并核对对比度",
                ))
            for element_id in slide.get("broken_images") or []:
                issues.append(_issue(
                    "broken_image",
                    "图片在 Chromium 中未成功解码",
                    slide_id=slide_id,
                    element_id=str(element_id),
                    evidence="image.complete=false 或 naturalWidth=0",
                    suggestion="检查冻结资源、媒体类型与图片内容",
                ))
        return issues
