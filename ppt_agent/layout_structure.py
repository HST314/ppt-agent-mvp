from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from .design_contract import LayoutSignature, TemplateRegistry


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list["_Node"] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def descendants(self) -> list["_Node"]:
        result: list[_Node] = []
        pending = list(reversed(self.children))
        while pending:
            node = pending.pop()
            result.append(node)
            pending.extend(reversed(node.children))
        return result


class _StructureParser(HTMLParser):
    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        node = _Node(tag, {str(name).lower(): value or "" for name, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        target = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == target:
                del self.stack[index:]
                return


def _bounds_evidence(actual: int, minimum: int, maximum: int) -> str:
    return f"actual={actual}, expected={minimum}" if minimum == maximum else f"actual={actual}, expected={minimum}..{maximum}"


def _slide_errors(slide: _Node, signature: LayoutSignature, index: int, layout_id: str) -> list[str]:
    prefix = f"Slide {index} ({layout_id})"
    errors: list[str] = []
    missing_roots = [class_name for class_name in signature.root_classes if class_name not in slide.classes]
    if missing_roots:
        errors.append(f"{prefix}: root missing class(es) {', '.join('.' + item for item in missing_roots)}")

    descendants = slide.descendants()
    containers = [
        node
        for node in descendants
        if any(class_name in node.classes for class_name in signature.container_any_of)
    ]
    if len(containers) != 1:
        alternatives = " | ".join(f".{item}" for item in signature.container_any_of)
        errors.append(f"{prefix}: expected exactly one structural container ({alternatives}); actual={len(containers)}")
    elif signature.direct_children is not None:
        minimum, maximum = signature.direct_children
        actual = len(containers[0].children)
        if not minimum <= actual <= maximum:
            errors.append(
                f"{prefix}: structural container direct-child count mismatch; "
                + _bounds_evidence(actual, minimum, maximum)
            )

    for class_name, minimum, maximum in signature.required_classes:
        actual = sum(class_name in node.classes for node in descendants)
        if not minimum <= actual <= maximum:
            errors.append(
                f"{prefix}: required .{class_name} count mismatch; "
                + _bounds_evidence(actual, minimum, maximum)
            )
    return errors


def run_layout_structure_validator(html_text: str, style_id: str) -> dict[str, Any]:
    """Validate registered layout IDs against their server-owned DOM signatures."""
    template = TemplateRegistry().resolve(style_id)
    if not template.layout_signatures:
        return {
            "applicable": False,
            "passed": True,
            "checked_slide_count": 0,
            "matched_slide_count": 0,
            "errors": [],
            "layouts": [],
        }

    parser = _StructureParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return {
            "applicable": True,
            "passed": False,
            "checked_slide_count": 0,
            "matched_slide_count": 0,
            "errors": ["layout structural signature parser failed"],
            "layouts": [],
        }

    slides = [node for node in parser.root.descendants() if node.tag == "section" and "slide" in node.classes]
    errors: list[str] = []
    layouts: list[dict[str, Any]] = []
    matched = 0
    for index, slide in enumerate(slides, 1):
        layout_id = slide.attrs.get("data-layout", "")
        signature = template.layout_signatures.get(layout_id)
        if signature is None:
            # The canonical validator owns missing/unregistered layout errors.
            layouts.append({"index": index, "layout_id": layout_id, "passed": False})
            continue
        slide_errors = _slide_errors(slide, signature, index, layout_id)
        matched += not slide_errors
        errors.extend(slide_errors)
        layouts.append({"index": index, "layout_id": layout_id, "passed": not slide_errors})
    if not slides:
        errors.append("No <section class=\"slide\"> pages found for layout structural signatures")
    return {
        "applicable": True,
        "passed": bool(slides) and not errors and matched == len(slides),
        "checked_slide_count": len(slides),
        "matched_slide_count": int(matched),
        "errors": errors,
        "layouts": layouts,
    }
