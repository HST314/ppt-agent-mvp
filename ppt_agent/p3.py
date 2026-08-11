from __future__ import annotations

import re

from .errors import ValidationError

SLIDE_HEADING = re.compile(r"^##\s+\[(slide-[A-Za-z0-9_-]+)\]\s+(.+?)\s*$", re.MULTILINE)
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


def parse_outline(markdown, allowed_resources, expected_count=None):
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValidationError("逐页大纲不得为空")
    matches = list(SLIDE_HEADING.finditer(markdown))
    slide_ids = [match.group(1) for match in matches]
    if not slide_ids:
        raise ValidationError("逐页大纲必须包含 ## [slide-N] 页面标题")
    if len(slide_ids) != len(set(slide_ids)):
        raise ValidationError("页面 ID 不得重复")
    if expected_count is not None and len(slide_ids) != expected_count:
        raise ValidationError(f"逐页大纲必须严格包含 {expected_count} 页")
    allowed = {item["uri"] for item in allowed_resources}
    unknown = sorted(set(RESOURCE_REF.findall(markdown)) - allowed)
    if unknown:
        raise ValidationError("大纲引用了冻结资源清单之外的资源")
    blocks = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        blocks[match.group(1)] = markdown[match.start():end].strip()
    return slide_ids, blocks


def changed_slide_ids(before, after):
    return sorted({*before, *after} - {sid for sid in before if sid in after and before[sid] == after[sid]})
