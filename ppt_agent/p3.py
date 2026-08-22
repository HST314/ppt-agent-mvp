from __future__ import annotations

import json
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
NARRATIVE_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
NARRATIVE_MIN_SECTION_COUNT = 2
NARRATIVE_MIN_BODY_CHARACTERS = 60


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
    return (f"# 整稿叙事结构\n\n## 核心结论\n{card.get('topic') or '待明确主题'}应服务于{card.get('goal') or '演示目标'}，"
            "以已确认事实形成清晰、可验证的决策依据。\n\n"
            f"## 叙事路径\n面向{card.get('audience') or '目标受众'}，依次建立背景、核心方案与行动建议，"
            "让每一章节都推进同一核心判断。\n\n"
            "## 章节组织\n1. 背景与目标\n2. 核心内容与证据\n3. 总结、决策与行动\n")


def _semantic_text(value):
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", str(value)).casefold()


def narrative_quality_evidence(markdown, card):
    """Return the deterministic minimum semantic/structure contract.

    This is intentionally a floor rather than an editorial score.  It rejects
    empty, deferred, or one-line meta answers before they can become planning
    artifacts, while leaving wording and section names to the model or user.
    """
    if not isinstance(markdown, str):
        markdown = ""
    headings = list(NARRATIVE_HEADING.finditer(markdown))
    level_one = [match for match in headings if len(match.group(1)) == 1]
    sections = [match for match in headings if len(match.group(1)) == 2]
    section_bodies = []
    for index, match in enumerate(sections):
        end = sections[index + 1].start() if index + 1 < len(sections) else len(markdown)
        body = re.sub(r"^#{1,6}\s+.*$", "", markdown[match.end():end], flags=re.MULTILINE)
        body = re.sub(r"[`*_>\-#|]", "", body)
        section_bodies.append(_semantic_text(body))
    body_text = re.sub(r"^#{1,6}\s+.*$", "", markdown, flags=re.MULTILINE)
    body_characters = len(_semantic_text(body_text))
    required_context = []
    for field in ("topic", "goal", "audience"):
        value = card.get(field) if isinstance(card, dict) else None
        token = _semantic_text(value) if isinstance(value, str) else ""
        # Single-character synthetic values used by low-level tests are not a
        # meaningful semantic contract for a real narrative.
        if len(token) >= 2:
            # Frozen task context is an identity contract, not a fuzzy topic
            # match.  Requiring the literal value prevents a correction from
            # silently paraphrasing or compacting user-owned wording.
            required_context.append({"field": field, "value": value, "covered": value in body_text})
    missing_context = [item for item in required_context if not item["covered"]]
    issues = []
    if not markdown.strip():
        issues.append("Markdown 不得为空")
    if not level_one:
        issues.append("必须包含一级标题")
    if len(sections) < NARRATIVE_MIN_SECTION_COUNT:
        issues.append(f"必须包含至少 {NARRATIVE_MIN_SECTION_COUNT} 个二级叙事章节")
    if len([body for body in section_bodies if len(body) >= 8]) < NARRATIVE_MIN_SECTION_COUNT:
        issues.append("至少两个叙事章节必须包含实质正文")
    if body_characters < NARRATIVE_MIN_BODY_CHARACTERS:
        issues.append(f"正文有效字符不得少于 {NARRATIVE_MIN_BODY_CHARACTERS}")
    if missing_context:
        issues.append("必须显式覆盖任务主题、目标与受众：" + "、".join(item["field"] for item in missing_context))
    return {
        "passed": not issues,
        "issues": issues,
        "h1_count": len(level_one),
        "section_count": len(sections),
        "substantive_section_count": len([body for body in section_bodies if len(body) >= 8]),
        "body_character_count": body_characters,
        "minimum_section_count": NARRATIVE_MIN_SECTION_COUNT,
        "minimum_body_characters": NARRATIVE_MIN_BODY_CHARACTERS,
        "required_context": required_context,
        "missing_context_fields": [item["field"] for item in missing_context],
    }


def assert_narrative_quality(markdown, card):
    evidence = narrative_quality_evidence(markdown, card)
    if not evidence["passed"]:
        raise ValidationError("叙事最低语义/结构门禁未通过：" + "；".join(evidence["issues"]))
    return evidence


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
        if index == min(1, count - 1) and card.get("constraints"):
            # The deterministic fallback must preserve the same required-claim
            # coverage contract as the model path.  Keeping confirmed source
            # facts in one auditable outline block also lets sample selection
            # derive the exact subset it must render.
            facts = json.dumps(card["constraints"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            blocks.append(f"- 已确认输入事实：{facts}")
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
