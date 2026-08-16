from __future__ import annotations

import re

from .errors import ValidationError

SLIDE_HEADING = re.compile(r"^##\s+\[(slide-[A-Za-z0-9_-]+)\]\s+(.+?)\s*$", re.MULTILINE)
MARKDOWN_HEADING = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.MULTILINE)
EXPLICIT_SLIDE_TITLE = re.compile(r"^\[(slide-[A-Za-z0-9_-]+)\]\s+(.+?)\s*$")
NUMBERED_PAGE_TITLE = re.compile(
    r"^(?:(?:第\s*\d+\s*页|(?:slide|page)\s*\d+)\s*[|｜:：.、)）\]】\-—]*\s*|\d+\s*[.、)）\]】]\s*)",
    re.IGNORECASE,
)
RESOURCE_REF = re.compile(r"!\[[^\]]*\]\((resources://[^)]+)\)")


def requested_slide_count(card):
    constraints = card.get("constraints") or {}
    for key in ("页数", "slide_count", "slides", "page_count"):
        if key in constraints:
            match = re.search(r"\d+", str(constraints[key]))
            if match:
                value = int(match.group())
                if value < 1 or value > 200:
                    raise ValidationError("强页数必须在 1 到 200 之间")
                return value
    return None


def narrative_markdown(card):
    return (f"# 整稿叙事结构\n\n## 核心结论\n{card.get('topic') or '待明确主题'}应服务于{card.get('goal') or '演示目标'}。\n\n"
            f"## 叙事路径\n面向{card.get('audience') or '目标受众'}，依次建立背景、核心方案与行动建议。\n\n"
            "## 章节组织\n1. 背景与目标\n2. 核心内容\n3. 总结与行动\n")


def outline_markdown(card, resources, count=None):
    count = count or max(3, min(8, len(resources) or 3))
    titles = ["开场与目标", "背景与挑战", "核心方案", "价值与证据", "行动建议", "总结"]
    blocks = ["# 逐页大纲", ""]
    for index in range(count):
        sid = f"slide-{index + 1}"
        blocks += [f"## [{sid}] {titles[index] if index < len(titles) else f'内容 {index + 1}'}",
                   f"- 页面目的：推进第 {index + 1} 个叙事节点",
                   f"- 主要内容：{card.get('topic') or '核心主题'}"]
        if index < len(resources):
            item = resources[index]
            blocks.append(f"- 视觉资源：![{item.get('description') or sid}]({item['uri']})")
        else:
            blocks.append("- 视觉资源：待补资源位（可无图片生成）")
        blocks.append("")
    return "\n".join(blocks)


def _allowed_resource_uris(resources):
    return {item["uri"] for item in resources}


def _validate_resource_refs(markdown, allowed_resources):
    unknown = sorted(set(RESOURCE_REF.findall(markdown)) - _allowed_resource_uris(allowed_resources))
    if unknown:
        raise ValidationError("大纲引用了冻结资源清单之外的资源")


def _normalized_outline(markdown, allowed_resources, expected_count=None):
    """Accept human-friendly Markdown and return the canonical machine form.

    Existing ``## [slide-N]`` headings retain their IDs.  Plain level-two
    headings and explicitly numbered page headings receive deterministic IDs,
    keeping Markdown as the editable artifact without requiring humans or a
    model to hand-author machine identifiers.
    """
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValidationError("逐页大纲不得为空")
    headings = []
    for match in MARKDOWN_HEADING.finditer(markdown):
        level, raw_title = len(match.group(1)), match.group(2).strip()
        explicit = EXPLICIT_SLIDE_TITLE.fullmatch(raw_title)
        # Arbitrary H3-H6 headings remain content unless they explicitly say
        # that they start a numbered page.  This avoids splitting sub-sections.
        if level > 2 and explicit is None and NUMBERED_PAGE_TITLE.match(raw_title) is None:
            continue
        title = explicit.group(2).strip() if explicit else NUMBERED_PAGE_TITLE.sub("", raw_title, count=1).strip()
        if not title:
            raise ValidationError("Markdown 页面标题不得为空")
        headings.append((match, explicit.group(1) if explicit else None, title))
    if not headings:
        raise ValidationError("逐页大纲必须包含 Markdown 页面标题，例如 ## 开场 或 ## [slide-1] 开场")
    explicit_ids = [sid for _, sid, _ in headings if sid]
    if len(explicit_ids) != len(set(explicit_ids)):
        raise ValidationError("页面 ID 不得重复")
    used = set(explicit_ids)
    slide_ids = []
    for index, (_, sid, _) in enumerate(headings):
        if sid:
            slide_ids.append(sid)
            continue
        candidate, suffix = f"slide-{index + 1}", 1
        while candidate in used:
            candidate, suffix = f"slide-{suffix}", suffix + 1
        used.add(candidate); slide_ids.append(candidate)
    if expected_count is not None and len(slide_ids) != expected_count:
        raise ValidationError(f"逐页大纲必须严格包含 {expected_count} 页")
    _validate_resource_refs(markdown, allowed_resources)

    prefix = markdown[:headings[0][0].start()].strip() or "# 逐页大纲"
    canonical_blocks = []
    for index, (match, _, title) in enumerate(headings):
        end = headings[index + 1][0].start() if index + 1 < len(headings) else len(markdown)
        body = markdown[match.end():end].strip()
        canonical_blocks.append(f"## [{slide_ids[index]}] {title}" + (f"\n{body}" if body else ""))
    canonical = prefix + "\n\n" + "\n\n".join(canonical_blocks) + "\n"
    blocks = {}
    matches = list(SLIDE_HEADING.finditer(canonical))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(canonical)
        blocks[match.group(1)] = canonical[match.start():end].strip()
    return canonical, slide_ids, blocks


def normalize_outline_markdown(markdown, allowed_resources, expected_count=None):
    return _normalized_outline(markdown, allowed_resources, expected_count)[0]


def parse_outline(markdown, allowed_resources, expected_count=None):
    _, slide_ids, blocks = _normalized_outline(markdown, allowed_resources, expected_count)
    return slide_ids, blocks


def structured_outline_markdown(slides, allowed_resources, expected_count=None):
    """Validate structured model output and deterministically render Markdown."""
    if not isinstance(slides, list) or not slides:
        raise ValidationError("结构化大纲必须包含至少一页 slides")
    if expected_count is not None and len(slides) != expected_count:
        raise ValidationError(f"结构化大纲必须严格包含 {expected_count} 页")
    allowed = _allowed_resource_uris(allowed_resources)
    required = {"title", "purpose", "content_markdown", "resource_uris"}
    blocks = ["# 逐页大纲", ""]
    for index, slide in enumerate(slides):
        if not isinstance(slide, dict) or set(slide) != required:
            raise ValidationError(f"结构化大纲第 {index + 1} 页字段无效")
        title, purpose, content = (slide[name] for name in ("title", "purpose", "content_markdown"))
        if any(not isinstance(value, str) or not value.strip() for value in (title, purpose, content)):
            raise ValidationError(f"结构化大纲第 {index + 1} 页标题、目的和内容不得为空")
        if "\n" in title or "\r" in title:
            raise ValidationError(f"结构化大纲第 {index + 1} 页标题必须为单行文本")
        if re.search(r"^#{1,2}\s+", content, re.MULTILINE):
            raise ValidationError(f"结构化大纲第 {index + 1} 页内容不得包含一级或二级标题")
        resource_uris = slide["resource_uris"]
        if not isinstance(resource_uris, list) or any(not isinstance(uri, str) for uri in resource_uris):
            raise ValidationError(f"结构化大纲第 {index + 1} 页 resource_uris 必须为字符串数组")
        if len(resource_uris) != len(set(resource_uris)):
            raise ValidationError(f"结构化大纲第 {index + 1} 页资源不得重复")
        if set(resource_uris) - allowed:
            raise ValidationError(f"结构化大纲第 {index + 1} 页引用了冻结资源清单之外的资源")
        _validate_resource_refs(content, allowed_resources)
        blocks.extend([
            f"## [slide-{index + 1}] {title.strip()}",
            f"- 页面目的：{' '.join(purpose.split())}",
            f"- 主要内容：\n{content.strip()}",
        ])
        if resource_uris:
            blocks.extend(f"- 视觉资源：![资源]({uri})" for uri in resource_uris)
        else:
            blocks.append("- 视觉资源：待补资源位（可无图片生成）")
        blocks.append("")
    markdown = "\n".join(blocks)
    return normalize_outline_markdown(markdown, allowed_resources, expected_count)


def changed_slide_ids(before, after):
    return sorted({*before, *after} - {sid for sid in before if sid in after and before[sid] == after[sid]})
