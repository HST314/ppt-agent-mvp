from __future__ import annotations

import base64
import hashlib
import time
from typing import Any


VIEWPORT = {"width": 1280, "height": 720}
TOLERANCE_PX = 1.0
MIN_TITLE_PX = 32.0
MIN_BODY_PX = 16.0
MIN_META_PX = 12.0
VISUAL_CAPTURE_MEDIA_TYPE = "image/webp"
VISUAL_CAPTURE_QUALITY = 72


def _issue(code: str, message: str, *, severity: str = "blocker", slide_id: str = "", element_id: str = "", evidence: str, suggestion: str, selector: str = "", geometry: dict | None = None) -> dict:
    stable = hashlib.sha256(f"{code}\0{slide_id}\0{element_id}\0{evidence}".encode()).hexdigest()[:12]
    issue = {
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
    if selector:
        issue["selector"] = selector
    if geometry:
        issue["geometry"] = geometry
    return issue


def _visual_quality(slides: list[dict]) -> tuple[dict, list[dict]]:
    """Score compositional signals that geometry-only gates cannot express.

    The score is deliberately advisory: deterministic contract/overflow checks
    remain the hard gate, while these heuristics surface suspicious whitespace,
    balance and deck-level repetition as warnings for human review.
    """
    metrics = []
    issues = []
    for slide in slides:
        slide_id = str(slide.get("slide_id") or "")
        role = str(slide.get("visual_role") or "content")
        coverage = max(0.0, min(1.0, float(slide.get("content_coverage") or 0)))
        balance = max(0.0, min(1.0, float(slide.get("balance_offset") or 0)))
        minimum = 0.08 if role in {"hero", "cover", "closing"} else 0.14
        coverage_score = min(100.0, coverage / minimum * 100.0) if coverage < minimum else 100.0
        if coverage > 0.82:
            coverage_score = max(55.0, 100.0 - (coverage - 0.82) * 250.0)
        balance_floor = 0.32 if role in {"hero", "cover", "closing"} else 0.24
        balance_score = 100.0 if balance <= balance_floor else max(35.0, 100.0 - (balance - balance_floor) * 180.0)
        score = round(coverage_score * 0.58 + balance_score * 0.42, 1)
        metrics.append({
            "slide_id": slide_id,
            "visual_role": role,
            "score": score,
            "content_coverage": round(coverage, 4),
            "content_bounds_coverage": round(float(slide.get("content_bounds_coverage") or 0), 4),
            "balance_offset": round(balance, 4),
            "meaningful_element_count": int(slide.get("meaningful_element_count") or 0),
            "layout_id": str(slide.get("layout_id") or ""),
            "theme": str(slide.get("theme") or "unknown"),
            "geometry_signature": str(slide.get("geometry_signature") or ""),
        })
        if coverage < minimum:
            issues.append(_issue(
                "excessive_whitespace",
                "页面有效视觉内容占比偏低",
                severity="warning",
                slide_id=slide_id,
                evidence=f"meaningful coverage={coverage:.1%}; advisory minimum={minimum:.0%}; role={role}",
                suggestion="扩大核心视觉或信息组，收紧无意图空白；若为刻意留白，请在人工审核中确认",
            ))
        imbalance_limit = 0.56 if role in {"hero", "cover", "closing"} else 0.46
        if balance > imbalance_limit:
            issues.append(_issue(
                "visual_imbalance",
                "页面视觉重心明显偏离画布中心",
                severity="warning",
                slide_id=slide_id,
                evidence=f"normalized centroid offset={balance:.3f}; advisory maximum={imbalance_limit:.2f}; role={role}",
                suggestion="检查主标题、数据图形与留白的相互制衡，避免内容无意集中在单一角落",
            ))

    slide_count = len(metrics)
    signatures = [item["geometry_signature"] for item in metrics if item["geometry_signature"]]
    unique_signatures = len(set(signatures))
    diversity_ratio = 1.0 if slide_count < 3 else unique_signatures / max(1, len(signatures))
    diversity_score = 100.0 if slide_count < 3 else min(100.0, diversity_ratio / 0.6 * 100.0)
    themes = [item["theme"] for item in metrics if item["theme"] != "unknown"]
    unique_themes = len(set(themes))
    theme_score = 100.0 if slide_count < 4 else min(100.0, unique_themes / 2 * 100.0)
    if slide_count >= 4 and diversity_ratio < 0.5:
        issues.append(_issue(
            "repetitive_layout",
            "整稿页面构图重复度偏高",
            severity="warning",
            evidence=f"unique geometry signatures={unique_signatures}/{slide_count}; diversity={diversity_ratio:.1%}",
            suggestion="为封面、复杂信息页和结论页使用不同视觉角色与登记布局",
        ))
    if slide_count >= 4 and unique_themes < 2:
        issues.append(_issue(
            "flat_theme_rhythm",
            "整稿缺少可辨识的主题节奏变化",
            severity="warning",
            evidence=f"distinct rendered themes={unique_themes}; slides={slide_count}",
            suggestion="按 DesignContract 核对 hero、light、dark 或 accent 页面节奏；单主题是明确设计决策时可人工确认",
        ))
    composition_score = sum(item["score"] for item in metrics) / slide_count if slide_count else 0.0
    overall = round(composition_score * 0.65 + diversity_score * 0.2 + theme_score * 0.15, 1)
    grade = "excellent" if overall >= 90 else "good" if overall >= 80 else "review" if overall >= 70 else "weak"
    return {
        "schema_version": "1.0",
        "score": overall,
        "grade": grade,
        "advisory": True,
        "scoring": {
            "composition_weight": 0.65,
            "layout_diversity_weight": 0.2,
            "theme_rhythm_weight": 0.15,
        },
        "composition_score": round(composition_score, 1),
        "layout_diversity_score": round(diversity_score, 1),
        "theme_rhythm_score": round(theme_score, 1),
        "unique_layout_signatures": unique_signatures,
        "distinct_themes": unique_themes,
        "slides": metrics,
        "screenshots": [],
    }, issues


class ChromiumDeckInspector:
    """Fail-closed DOM geometry checks using the production browser engine."""

    enforce_on_generation = True

    def __init__(self, *, timeout_ms: int = 15_000):
        self.timeout_ms = timeout_ms

    def inspect(self, html_text: str, expected_slide_ids: list[str], *, visual_quality: bool = False) -> dict:
        started = time.monotonic()
        try:
            raw = self._measure(html_text, capture_screenshots=visual_quality)
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
        visual = None
        screenshot_payloads = raw.pop("_screenshots", [])
        if visual_quality:
            visual, visual_issues = _visual_quality(raw["slides"])
            issues.extend(visual_issues)
            screenshots = []
            for item in screenshot_payloads:
                content = item["content"]
                screenshots.append({
                    "slide_id": item["slide_id"],
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "byte_size": len(content),
                    "media_type": VISUAL_CAPTURE_MEDIA_TYPE,
                    "width": VIEWPORT["width"],
                    "height": VIEWPORT["height"],
                })
            visual["screenshots"] = screenshots
        result = {
            "available": True,
            # Visual-quality findings are advisory.  Keep them in ``issues``
            # for review, but do not let a warning rewrite the technical hard
            # gate's pass/fail result.
            "passed": not any(item.get("severity") == "blocker" for item in issues),
            "engine": "chromium",
            "engine_version": raw["engine_version"],
            "viewport": dict(VIEWPORT),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            "issues": issues,
            "slides": raw["slides"],
        }
        if visual is not None:
            result["visual_quality"] = visual
            result["_visual_screenshots"] = screenshot_payloads
        return result

    def _measure(self, html_text: str, *, capture_screenshots: bool = False) -> dict[str, Any]:
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
                            const safeId = value => /^[A-Za-z0-9_-]+$/.test(value || '');
                            const selectorFor = element => {
                                const slideId = slide.getAttribute('data-slide-id') || slide.id;
                                const scope = safeId(slideId) ? `.slide[data-slide-id="${slideId}"]` : `.slide:nth-of-type(${slideIndex + 1})`;
                                const elementId = element.getAttribute('data-element-id');
                                if (safeId(elementId) && slide.querySelectorAll(`[data-element-id="${elementId}"]`).length === 1) {
                                    return `${scope} [data-element-id="${elementId}"]`;
                                }
                                const parts = [];
                                let node = element;
                                while (node && node !== slide) {
                                    const parent = node.parentElement;
                                    const siblings = [...parent.children].filter(child => child.tagName === node.tagName);
                                    parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${siblings.indexOf(node) + 1})`);
                                    node = parent;
                                }
                                return `${scope} > ${parts.join(' > ')}`;
                            };
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
                            const overflows = [...overflowRoots.entries()].map(([element, value]) => ({
                                selector: selectorFor(element),
                                ...value,
                            }));
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
                                        selector: selectorFor(element),
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
                            const ownText = element => [...element.childNodes].some(node => node.nodeType === Node.TEXT_NODE && (node.textContent || '').trim());
                            const meaningful = descendants.filter(element => {
                                const box = element.getBoundingClientRect();
                                const intersects = box.right > rect.left && box.left < rect.right && box.bottom > rect.top && box.top < rect.bottom;
                                if (!intersects) return false;
                                const tag = element.tagName.toLowerCase();
                                const visual = /^(img|svg|canvas|table|video|picture)$/.test(tag);
                                const semantic = /^(h[1-6]|p|li|td|th|blockquote|figcaption|small)$/.test(tag) ||
                                    element.hasAttribute('data-element-id') || element.hasAttribute('data-visual-role');
                                return visual || (semantic && (ownText(element) || !element.children.length));
                            }).map(element => {
                                const box = element.getBoundingClientRect();
                                const left = Math.max(rect.left, box.left), right = Math.min(rect.right, box.right);
                                const top = Math.max(rect.top, box.top), bottom = Math.min(rect.bottom, box.bottom);
                                return {tag: element.tagName.toLowerCase(), left, right, top, bottom,
                                    x: (left + right) / 2, y: (top + bottom) / 2};
                            }).filter(box => box.right > box.left && box.bottom > box.top);
                            const cols = 32, rows = 18, occupied = [];
                            for (let row = 0; row < rows; row += 1) {
                                for (let col = 0; col < cols; col += 1) {
                                    const x = rect.left + (col + .5) * rect.width / cols;
                                    const y = rect.top + (row + .5) * rect.height / rows;
                                    if (meaningful.some(box => x >= box.left && x <= box.right && y >= box.top && y <= box.bottom)) occupied.push({col, row});
                                }
                            }
                            const contentCoverage = occupied.length / (cols * rows);
                            const centerX = occupied.length ? occupied.reduce((sum, cell) => sum + (cell.col + .5) / cols, 0) / occupied.length : .5;
                            const centerY = occupied.length ? occupied.reduce((sum, cell) => sum + (cell.row + .5) / rows, 0) / occupied.length : .5;
                            const balanceOffset = Math.hypot(centerX - .5, centerY - .5) / Math.SQRT1_2;
                            const minLeft = meaningful.length ? Math.min(...meaningful.map(box => box.left)) : rect.left;
                            const maxRight = meaningful.length ? Math.max(...meaningful.map(box => box.right)) : rect.left;
                            const minTop = meaningful.length ? Math.min(...meaningful.map(box => box.top)) : rect.top;
                            const maxBottom = meaningful.length ? Math.max(...meaningful.map(box => box.bottom)) : rect.top;
                            const boundsCoverage = meaningful.length ? ((maxRight - minLeft) * (maxBottom - minTop)) / (rect.width * rect.height) : 0;
                            const geometrySignature = meaningful.slice(0, 16).map(box => {
                                const gx = Math.max(0, Math.min(5, Math.floor((box.x - rect.left) / rect.width * 6)));
                                const gy = Math.max(0, Math.min(3, Math.floor((box.y - rect.top) / rect.height * 4)));
                                return `${box.tag}:${gx},${gy}`;
                            }).sort().join('|');
                            const background = getComputedStyle(slide).backgroundColor;
                            const rgb = (background.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
                            const luminance = rgb.length === 3 ? (rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722) : 255;
                            const theme = slide.classList.contains('accent') ? 'accent' : slide.classList.contains('dark') || luminance < 80 ? 'dark' : slide.classList.contains('grey') ? 'grey' : 'light';
                            const visualRole = slide.getAttribute('data-visual-role') || (slide.classList.contains('hero') ? 'hero' : slideIndex === 0 ? 'cover' : slideIndex === slides.length - 1 ? 'closing' : 'content');
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
                                visual_role: visualRole,
                                layout_id: slide.getAttribute('data-layout') || '',
                                theme,
                                content_coverage: contentCoverage,
                                content_bounds_coverage: boundsCoverage,
                                balance_offset: balanceOffset,
                                meaningful_element_count: meaningful.length,
                                geometry_signature: geometrySignature,
                            };
                        })""",
                    )
                    screenshots = []
                    if capture_screenshots:
                        page.add_style_tag(content="""
                            body.visual-qa-capture{margin:0!important;overflow:hidden!important}
                            body.visual-qa-capture #deck{transform:none!important;transition:none!important}
                            body.visual-qa-capture .slide{visibility:hidden!important;pointer-events:none!important}
                            body.visual-qa-capture .slide[data-visual-qa-active="true"]{display:flex!important;visibility:visible!important;position:fixed!important;inset:0!important;margin:0!important;transform:none!important}
                            body.visual-qa-capture canvas.bg,body.visual-qa-capture #hint,body.visual-qa-capture nav{display:none!important}
                        """)
                        page.evaluate("document.body.classList.add('visual-qa-capture')")
                        for index, slide in enumerate(slides):
                            page.eval_on_selector_all(
                                ".slide",
                                "(nodes, active) => nodes.forEach((node, index) => node.setAttribute('data-visual-qa-active', String(index === active)))",
                                index,
                            )
                            png = page.screenshot(
                                type="png",
                                full_page=False,
                                animations="disabled",
                                caret="hide",
                            )
                            encoded = base64.b64encode(png).decode("ascii")
                            webp = page.evaluate(
                                """async ({encoded, quality}) => {
                                    const response = await fetch(`data:image/png;base64,${encoded}`);
                                    const bitmap = await createImageBitmap(await response.blob());
                                    const canvas = document.createElement('canvas');
                                    canvas.width = bitmap.width; canvas.height = bitmap.height;
                                    canvas.getContext('2d', {alpha: false}).drawImage(bitmap, 0, 0);
                                    bitmap.close();
                                    return canvas.toDataURL('image/webp', quality / 100);
                                }""",
                                {"encoded": encoded, "quality": VISUAL_CAPTURE_QUALITY},
                            )
                            content = base64.b64decode(webp.split(",", 1)[1])
                            screenshots.append({"slide_id": str(slide.get("slide_id") or ""), "content": content})
                        page.evaluate("""() => {
                            document.body.classList.remove('visual-qa-capture');
                            document.querySelectorAll('.slide').forEach(node => node.removeAttribute('data-visual-qa-active'));
                        }""")
                    return {"engine_version": browser.version, "slides": slides, "_screenshots": screenshots}
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
                    selector=str(item.get("selector") or ""),
                    geometry={
                        "overflow_px": round(overflow, 2),
                        "delta": {key: round(float(item.get(key) or 0), 2) for key in ("left", "top", "right", "bottom")},
                    },
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
                    selector=str(item.get("selector") or ""),
                    geometry={
                        "client_width": item.get("client_width"),
                        "client_height": item.get("client_height"),
                        "scroll_width": item.get("scroll_width"),
                        "scroll_height": item.get("scroll_height"),
                    },
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
