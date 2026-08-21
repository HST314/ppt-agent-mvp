"""Deterministic repair for slide geometry overflow.

The LLM occasionally emits slides whose content exceeds the 1280x720 canvas
(out-of-bounds elements) or an inner container (scroll overflow).  Those are
purely geometric defects: the fix does not need a model.  This module measures
the deck in Chromium — the same engine the inspector uses — and injects a
small, auditable ``<style data-overflow-autofit>`` block that zooms the
outermost offending roots until everything fits, then re-measures to verify.

Zoom semantics exploited here (Chromium applies ``zoom`` to used values):

* scroll overflow on element E: ``zoom: f`` scales E's box *and* content, so
  the scroll ratio alone never improves.  The rule therefore pairs the zoom
  with explicit compensated sizes (``width/height = target / f``), which pins
  E's visual box to the target while its content shrinks by ``f``.
* out-of-bounds element E: the same zoom shrinks E's used box toward the
  slide bounds.  Zoom also scales E's own top/left offsets, so an anchored
  box drifts toward the slide origin by ``(1 - f) * offset`` — always
  inward, never producing new out-of-bounds.
* an element flagged for *both* gets ONE unified rule: the factor is the
  content size measured against the clamped target box
  (``min(box, available) / scroll``), which satisfies the scroll and the
  bounds constraint simultaneously; separate per-kind rules oscillate.
* a small leaf with pure scroll overflow and measured slack next to it is
  NOT zoomed: the needed factor (e.g. 52px -> 61px content) sits under
  ``MIN_ZOOM`` and would also scale computed font sizes below the hard
  ``text_too_small`` gate.  The box is grown into the slack with an
  explicit compensated size instead, which changes no font metrics.

All declarations carry ``!important``: real decks size offending elements
through inline ``style=`` attributes, which otherwise silently win over the
injected stylesheet and leave the overflow un-repaired.

The block lives in ``<head>`` outside slide fragments, survives
``_replace_slide_fragments`` merges, is stripped and rebuilt from scratch on
every run, and uses only whitelisted CSS properties so the result still
passes ``validate_html``.
"""

from __future__ import annotations

import re

VIEWPORT = {"width": 1280, "height": 720}
TOLERANCE_PX = 1.0
SAFETY = 0.99
MIN_ZOOM = 0.85
MAX_RULES = 24
GROW_MARGIN_PX = 1.0

GEOMETRIC_CODES = ("content_out_of_bounds", "element_scroll_overflow")

_AUTOFIT_RE = re.compile(r"<style\b[^>]*\bdata-overflow-autofit\b[^>]*>[\s\S]*?</style\s*>", re.I)
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")


def strip_autofit_style(html_text: str) -> str:
    """Remove any previously injected autofit block (idempotent re-runs)."""
    return _AUTOFIT_RE.sub("", html_text)


def inject_autofit_style(html_text: str, css: str, marker: str) -> str:
    """Replace the autofit block with freshly computed rules."""
    stripped = strip_autofit_style(html_text)
    block = f'<style data-overflow-autofit="{marker}">\n{css}\n</style>'
    index = stripped.lower().rfind("</head>")
    if index >= 0:
        return stripped[:index] + block + stripped[index:]
    return stripped + block


_MEASURE_JS = r"""slides => slides.map((slide, slideIndex) => {
    const rect = slide.getBoundingClientRect();
    const visible = element => {
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' &&
            Number(style.opacity) !== 0 && box.width > 0 && box.height > 0;
    };
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
    const oobRoots = new Map();
    descendants.forEach(element => {
        const box = element.getBoundingClientRect();
        const delta = {
            left: rect.left - box.left,
            top: rect.top - box.top,
            right: box.right - rect.right,
            bottom: box.bottom - rect.bottom,
        };
        if (Math.max(delta.left, delta.top, delta.right, delta.bottom) > 1) {
            let ancestor = element.parentElement;
            let covered = false;
            while (ancestor && ancestor !== slide) {
                if (oobRoots.has(ancestor)) { covered = true; break; }
                ancestor = ancestor.parentElement;
            }
            if (!covered) oobRoots.set(element, { box, delta });
        }
    });
    const scrollRoots = new Set();
    descendants.forEach(element => {
        if (element.clientWidth > 0 && element.clientHeight > 0 &&
            (element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1)) {
            let ancestor = element.parentElement;
            let covered = false;
            while (ancestor && ancestor !== slide) {
                if (scrollRoots.has(ancestor)) { covered = true; break; }
                ancestor = ancestor.parentElement;
            }
            if (!covered) scrollRoots.add(element);
        }
    });
    // Room a box can grow into without overlapping the next visible element
    // stacked against it (horizontal overlap for downward growth, vertical
    // overlap for rightward growth).  Falls back to the slide edge.
    const clearanceFor = element => {
        const box = element.getBoundingClientRect();
        let below = rect.bottom - box.bottom;
        let right = rect.right - box.right;
        descendants.forEach(other => {
            if (other === element || element.contains(other) || other.contains(element)) return;
            const otherBox = other.getBoundingClientRect();
            const horizontalOverlap = Math.min(box.right, otherBox.right) - Math.max(box.left, otherBox.left);
            if (horizontalOverlap > 1 && otherBox.top >= box.bottom - 1) {
                below = Math.min(below, otherBox.top - box.bottom);
            }
            const verticalOverlap = Math.min(box.bottom, otherBox.bottom) - Math.max(box.top, otherBox.top);
            if (verticalOverlap > 1 && otherBox.left >= box.right - 1) {
                right = Math.min(right, otherBox.left - box.right);
            }
        });
        return { below: Math.max(0, below), right: Math.max(0, right) };
    };
    const targets = [];
    oobRoots.forEach((value, element) => targets.push({
        kind: 'oob',
        selector: selectorFor(element),
        box: { left: value.box.left, top: value.box.top, width: value.box.width, height: value.box.height },
        delta: value.delta,
    }));
    scrollRoots.forEach(element => targets.push({
        kind: 'scroll',
        selector: selectorFor(element),
        box: (box => ({ left: box.left, top: box.top, width: box.width, height: box.height }))(element.getBoundingClientRect()),
        client: { width: element.clientWidth, height: element.clientHeight },
        scroll: { width: element.scrollWidth, height: element.scrollHeight },
        clear: clearanceFor(element),
    }));
    return {
        slide_id: slide.getAttribute('data-slide-id') || slide.id || '',
        index: slideIndex,
        rect: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
        slide_dims: {
            client_width: slide.clientWidth,
            client_height: slide.clientHeight,
            scroll_width: slide.scrollWidth,
            scroll_height: slide.scrollHeight,
        },
        targets: targets.map(target => ({ ...target, unique: document.querySelectorAll(target.selector).length === 1 })),
    };
})"""


def _describe(slide: dict, target: dict) -> dict:
    return {"slide_id": slide.get("slide_id") or "", "kind": target.get("kind"), "selector": target.get("selector")}


def _slide_scope(slide: dict) -> str:
    slide_id = slide.get("slide_id") or ""
    if _SAFE_ID.fullmatch(slide_id):
        return f'.slide[data-slide-id="{slide_id}"]'
    return f'.slide:nth-of-type({slide["index"] + 1})'


def _grow_declarations(target: dict) -> str | None:
    """Grow a pure scroll-overflow leaf into its measured slack.

    Small text leaves (a 52px label holding 61px of content is the typical
    real case) cannot be zoomed: the factor (~0.84) sits under ``MIN_ZOOM``
    and would also shrink computed font sizes below the hard
    ``text_too_small`` gate.  When the measured clearance shows the content
    fits by simply enlarging the box, pin explicit compensated sizes so no
    font metric changes.  Returns ``None`` when the slack is insufficient —
    the caller then falls back to the zoom path, keeping fail-closed
    behaviour for genuinely oversized content.
    """
    box = target["box"]
    content = target["scroll"]
    clear = target.get("clear") or {}
    grow_w = box["width"] + max(0.0, float(clear.get("right") or 0.0))
    grow_h = box["height"] + max(0.0, float(clear.get("below") or 0.0))
    if content["width"] + GROW_MARGIN_PX > grow_w or content["height"] + GROW_MARGIN_PX > grow_h:
        return None
    declarations = ["box-sizing: border-box !important", "flex: none !important"]
    if content["width"] > box["width"] + TOLERANCE_PX:
        declarations.append(f"width: {content['width'] + GROW_MARGIN_PX:.2f}px !important")
    if content["height"] > box["height"] + TOLERANCE_PX:
        declarations.append(f"height: {content['height'] + GROW_MARGIN_PX:.2f}px !important")
    if len(declarations) == 2:
        return None
    return "; ".join(declarations) + ";"


def _rules_for(measured: list[dict], known: dict[str, str]) -> tuple[dict[str, str], list[dict]]:
    """Compute per-selector CSS declarations for one measurement round.

    Returns (rules, unfixable).  Rules are keyed by selector so a residual
    element re-measured in a later round replaces its own stale rule instead
    of stacking conflicting declarations.

    An element flagged for out-of-bounds *and* scroll overflow gets ONE
    unified rule: the zoom satisfies both constraints at once and the
    compensated size pins the visual box to ``min(original, available)``
    rather than to the original box.  Splitting this into separate per-round
    rules oscillates forever (scroll fix keeps the box so the OOB persists;
    OOB fix shrinks the box so the scroll returns).
    """
    rules: dict[str, str] = {}
    unfixable: list[dict] = []
    budget = MAX_RULES - len(known)

    def claim(selector: str) -> bool:
        nonlocal budget
        if selector in known:
            return True
        if budget <= 0:
            return False
        budget -= 1
        return True

    for slide in measured:
        targets = slide.get("targets") or []
        dims = slide.get("slide_dims") or {}
        if (
            not targets
            and dims.get("client_height")
            and (dims["scroll_width"] > dims["client_width"] + 1 or dims["scroll_height"] > dims["client_height"] + 1)
        ):
            # Whole-canvas scroll with no single child root: shrink every
            # direct child so the flow shortens.  Only used when no child was
            # flagged — child fixes are tried first and re-measured.
            factor = min(1.0, dims["client_width"] / dims["scroll_width"], dims["client_height"] / dims["scroll_height"]) * SAFETY
            selector = f"{_slide_scope(slide)} > *"
            entry = {"slide_id": slide.get("slide_id") or "", "kind": "slide_scroll", "selector": selector}
            if factor < MIN_ZOOM:
                unfixable.append({**entry, "reason": "zoom_below_floor", "factor": round(factor, 4)})
            elif not claim(selector):
                unfixable.append({**entry, "reason": "rule_budget_exhausted"})
            else:
                rules[selector] = f"zoom: {factor:.4f} !important;"
            continue
        merged: dict[str, dict] = {}
        order: list[str] = []
        for target in targets:
            selector = target["selector"]
            if selector not in merged:
                merged[selector] = {"selector": selector, "unique": True, "scroll": None, "oob": None}
                order.append(selector)
            entry = merged[selector]
            entry["unique"] = entry["unique"] and bool(target.get("unique"))
            entry[target["kind"]] = target
        for selector in order:
            entry = merged[selector]
            scroll, oob = entry["scroll"], entry["oob"]
            describe = {"slide_id": slide.get("slide_id") or "", "kind": "scroll" if scroll else "oob", "selector": selector}
            if not entry["unique"]:
                unfixable.append({**describe, "reason": "selector_not_unique"})
                continue
            if oob and (oob["delta"]["left"] > TOLERANCE_PX or oob["delta"]["top"] > TOLERANCE_PX):
                unfixable.append({**describe, "reason": "negative_anchor"})
                continue
            if scroll and not oob:
                grown = _grow_declarations(scroll)
                if grown is not None:
                    if not claim(selector):
                        unfixable.append({**describe, "reason": "rule_budget_exhausted"})
                    else:
                        rules[selector] = grown
                    continue
            box = (scroll or oob)["box"]
            available_h = slide["rect"]["height"] - max(0.0, box["top"] - slide["rect"]["top"])
            available_w = slide["rect"]["width"] - max(0.0, box["left"] - slide["rect"]["left"])
            target_h = min(box["height"], available_h)
            target_w = min(box["width"], available_w)
            # 统一约束：缩放后的内容（scroll 尺寸）必须装进被钳制后的可视盒
            # (target)。纯越界时 content==box，退化为 available/box；纯滚动
            # 溢出时 target==box，退化为 client/scroll；复合情形取两者交集，
            # 避免修好一个约束又重新引入另一个。
            content_w = scroll["scroll"]["width"] if scroll else box["width"]
            content_h = scroll["scroll"]["height"] if scroll else box["height"]
            factor = 1.0
            if content_w:
                factor = min(factor, target_w / content_w)
            if content_h:
                factor = min(factor, target_h / content_h)
            factor *= SAFETY
            if factor >= 1.0 or factor < MIN_ZOOM:
                unfixable.append({**describe, "reason": "zoom_below_floor", "factor": round(factor, 4)})
                continue
            if not claim(selector):
                unfixable.append({**describe, "reason": "rule_budget_exhausted"})
                continue
            rules[selector] = (
                f"zoom: {factor:.4f} !important; box-sizing: border-box !important; flex: none !important; "
                f"width: {target_w / factor:.2f}px !important; height: {target_h / factor:.2f}px !important;"
            )
    return rules, unfixable


def _render_css(rules: dict[str, str]) -> str:
    return "\n".join(f"{selector} {{ {declarations} }}" for selector, declarations in rules.items())


def fit_deck_html(html_text: str, *, max_rounds: int = 2, timeout_ms: int = 15_000) -> dict:
    """Measure and repair geometric overflow; return the fitted HTML.

    The returned dict carries ``available`` (Chromium reachable), ``html``,
    ``rules`` (selector -> declarations actually applied), ``rounds``,
    ``converged`` (final measurement clean) and ``remaining`` (targets the
    deterministic path could not fix and which stay for LLM/manual repair).
    """
    base = strip_autofit_style(html_text)
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"available": False, "html": html_text, "rules": {}, "rounds": 0, "converged": False, "remaining": []}

    rules: dict[str, str] = {}
    rounds = 0
    remaining: list[dict] = []
    converged = False
    current = base
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport=VIEWPORT, reduced_motion="reduce")
                try:
                    page = context.new_page()
                    page.set_default_timeout(timeout_ms)
                    page.route("http://**/*", lambda route: route.abort())
                    page.route("https://**/*", lambda route: route.abort())

                    def measure(html: str) -> list[dict]:
                        page.set_content(html, wait_until="load", timeout=timeout_ms)
                        page.add_style_tag(content="*,*::before,*::after{animation:none!important;transition:none!important}")
                        page.evaluate("document.fonts && document.fonts.ready")
                        return page.eval_on_selector_all(".slide", _MEASURE_JS)

                    for _ in range(max(1, max_rounds) + 1):
                        measured = measure(current)
                        fresh, unfixable = _rules_for(measured, rules)
                        residual = [target for slide in measured for target in (slide.get("targets") or [])]
                        if not residual and not fresh:
                            converged = not unfixable
                            remaining = unfixable
                            break
                        if rounds >= max(1, max_rounds) or not fresh:
                            # 同一目标的残留描述与 unfixable 原因只保留后者（信息更全）。
                            explained = {item["selector"] for item in unfixable}
                            remaining = [
                                _describe(slide, target)
                                for slide in measured
                                for target in (slide.get("targets") or [])
                                if target.get("selector") not in explained
                            ] + unfixable
                            break
                        rules.update(fresh)
                        rounds += 1
                        current = inject_autofit_style(base, _render_css(rules), f"round-{rounds}")
                finally:
                    context.close()
            finally:
                browser.close()
    except Exception:
        return {"available": False, "html": html_text, "rules": {}, "rounds": 0, "converged": False, "remaining": []}

    if not rules:
        return {"available": True, "html": html_text, "rules": {}, "rounds": 0, "converged": converged, "remaining": remaining}
    return {"available": True, "html": current, "rules": rules, "rounds": rounds, "converged": converged, "remaining": remaining}
