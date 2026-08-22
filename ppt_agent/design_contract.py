from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError
from .skill_runtime import SkillRuntime


REGISTRY_PATH = "references/template-registry.json"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)\S")
_QUANTIFIED = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\d+(?:\.\d+)?\s*(?:%|％|万元|万|元|周|月|天|日|小时|分钟|人|项|个|次|倍|分)"
    r"|\d+(?:\.\d+)?\s*(?:→|->)\s*\d+(?:\.\d+)?"
    r"|[¥￥$]\s*\d)"
)


def _swiss_layout_for_outline(block: str, fallback_index: int) -> str:
    """Choose a registered Swiss skeleton from the page's content shape."""
    text = str(block or "")
    lowered = text.casefold()
    item_count = sum(
        1
        for line in text.splitlines()
        if _LIST_ITEM.search(line)
        and not re.match(r"^\s*[-*+]\s*(?:页面目的|主要内容|视觉资源)\s*[：:]", line)
    )
    quantified_count = len(_QUANTIFIED.findall(text))
    has_budget = bool(re.search(r"预算|成本|费用|报价|财务|支出|投入|budget|cost", lowered))
    has_timeline = bool(re.search(r"路线图|时间线|里程碑|阶段|实施|推进|排期|roadmap|timeline|milestone", lowered))
    has_comparison = bool(re.search(r"对比|比较|排名|占比|before|after|versus|\bvs\b", lowered))
    has_kpi = bool(re.search(r"\bkpi\b|指标|性能|效率|满意度|转接率|响应时间|达成率|增长率", lowered))
    has_image = "resources://" in lowered

    if has_image and quantified_count >= 3:
        return "S22"
    if has_budget and 4 <= max(item_count, quantified_count) <= 6:
        return "S20"
    if has_timeline:
        if item_count == 3:
            return "S05"
        if 4 <= item_count <= 7:
            return "S11"
        return "S11"
    if has_comparison and item_count == 2:
        return "S08"
    if has_kpi and item_count == 4 and quantified_count >= 4:
        return "S06"
    if has_comparison and 5 <= item_count <= 10 and quantified_count >= item_count:
        return "S07"
    if item_count == 6:
        return "S04"
    if item_count == 3:
        return "S05"
    if item_count == 4:
        return "S19"
    if 8 <= item_count <= 12:
        return "S15"
    if quantified_count >= 4:
        return "S20"
    # General-purpose fallbacks avoid data-only S06/S07/S20 when the page
    # does not actually carry comparable quantitative values.
    return ("S03", "S08", "S19")[fallback_index % 3]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class LayoutSignature:
    root_classes: tuple[str, ...]
    container_any_of: tuple[str, ...]
    direct_children: tuple[int, int] | None
    required_classes: tuple[tuple[str, int, int], ...]

    def public(self) -> dict[str, Any]:
        return {
            "root_classes": list(self.root_classes),
            "container_any_of": list(self.container_any_of),
            "direct_children": list(self.direct_children) if self.direct_children else None,
            "required_classes": {
                class_name: [minimum, maximum]
                for class_name, minimum, maximum in self.required_classes
            },
        }


@dataclass(frozen=True)
class TemplateRecord:
    style_id: str
    aliases: tuple[str, ...]
    template_id: str
    asset_path: str
    template_hash: str
    theme_id: str
    semantic_classes: tuple[str, ...]
    allowed_layouts: tuple[str, ...]
    body_layouts: tuple[str, ...]
    cover_layout: str
    closing_layout: str
    layout_signatures: dict[str, LayoutSignature]

    def public(self) -> dict[str, Any]:
        return {
            "style_id": self.style_id,
            "template_id": self.template_id,
            "asset_path": self.asset_path,
            "template_hash": self.template_hash,
            "theme_id": self.theme_id,
            "semantic_classes": list(self.semantic_classes),
            "allowed_layouts": list(self.allowed_layouts),
            "body_layouts": list(self.body_layouts),
            "cover_layout": self.cover_layout,
            "closing_layout": self.closing_layout,
            "layout_signatures": {
                layout_id: signature.public()
                for layout_id, signature in self.layout_signatures.items()
            },
        }


class TemplateRegistry:
    """Hash-locked registry for the real built-in template assets."""

    def __init__(self, skill: SkillRuntime | None = None):
        self.skill = skill or SkillRuntime.builtin()
        try:
            raw = json.loads(self.skill.read_locked_text(REGISTRY_PATH))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValidationError("模板注册表不是有效 JSON") from exc
        self.version = raw.get("version")
        self.default_style_id = raw.get("default_style_id")
        templates = raw.get("templates")
        if not isinstance(self.version, str) or not self.version or not isinstance(templates, list) or not templates:
            raise ValidationError("模板注册表结构无效")
        records: dict[str, TemplateRecord] = {}
        for item in templates:
            if not isinstance(item, dict):
                raise ValidationError("模板注册项必须是对象")
            required = {
                "style_id", "aliases", "template_id", "asset_path", "theme_id",
                "semantic_classes", "allowed_layouts", "body_layouts", "cover_layout", "closing_layout",
                "layout_signatures",
            }
            if set(item) != required:
                raise ValidationError("模板注册项字段无效")
            style_id = item["style_id"]
            path = item["asset_path"]
            allowed = tuple(item["allowed_layouts"])
            body = tuple(item["body_layouts"])
            semantic = tuple(item["semantic_classes"])
            values = (style_id, item["template_id"], item["theme_id"], *semantic, *allowed)
            if any(not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) for value in values):
                raise ValidationError("模板注册项标识无效")
            if style_id in records or path not in self.skill.manifest or not path.startswith("assets/"):
                raise ValidationError("模板注册项重复或未锁定")
            if not allowed or len(set(allowed)) != len(allowed) or not body or not set(body).issubset(allowed):
                raise ValidationError("模板布局登记无效")
            if not semantic or len(set(semantic)) != len(semantic):
                raise ValidationError("模板语义类登记无效")
            if item["cover_layout"] not in allowed or item["closing_layout"] not in allowed:
                raise ValidationError("模板封面或封底布局未登记")
            raw_signatures = item["layout_signatures"]
            if not isinstance(raw_signatures, dict):
                raise ValidationError("模板布局结构签名无效")
            if raw_signatures and set(raw_signatures) != set(allowed):
                raise ValidationError("模板布局结构签名必须完整覆盖已登记版式")
            signatures: dict[str, LayoutSignature] = {}
            for layout_id, signature in raw_signatures.items():
                if not isinstance(signature, dict) or set(signature) != {
                    "root_classes", "container_any_of", "direct_children", "required_classes",
                }:
                    raise ValidationError("模板布局结构签名字段无效")
                roots = signature["root_classes"]
                containers = signature["container_any_of"]
                direct = signature["direct_children"]
                required_classes = signature["required_classes"]
                if (
                    not isinstance(roots, list) or not roots
                    or not isinstance(containers, list) or not containers
                    or any(not isinstance(name, str) or not _IDENTIFIER.fullmatch(name) for name in (*roots, *containers))
                    or len(roots) != len(set(roots)) or len(containers) != len(set(containers))
                    or not isinstance(required_classes, dict)
                ):
                    raise ValidationError("模板布局结构签名类名无效")
                if direct is not None and (
                    not isinstance(direct, list) or len(direct) != 2
                    or any(not isinstance(value, int) or value < 0 for value in direct)
                    or direct[0] > direct[1]
                ):
                    raise ValidationError("模板布局结构签名子节点范围无效")
                class_rules = []
                for class_name, bounds in required_classes.items():
                    if (
                        not isinstance(class_name, str) or not _IDENTIFIER.fullmatch(class_name)
                        or not isinstance(bounds, list) or len(bounds) != 2
                        or any(not isinstance(value, int) or value < 0 for value in bounds)
                        or bounds[0] > bounds[1]
                    ):
                        raise ValidationError("模板布局结构签名数量范围无效")
                    class_rules.append((class_name, bounds[0], bounds[1]))
                signatures[layout_id] = LayoutSignature(
                    root_classes=tuple(roots),
                    container_any_of=tuple(containers),
                    direct_children=tuple(direct) if direct is not None else None,
                    required_classes=tuple(class_rules),
                )
            aliases = tuple(str(alias).strip().casefold() for alias in item["aliases"] if str(alias).strip())
            if not aliases:
                raise ValidationError("模板别名不得为空")
            template_css = self.skill.read_locked_text(path)
            missing_semantic = [
                name for name in semantic
                if not re.search(rf"\.slide(?:[.#:\[\]A-Za-z0-9_ -]*)\.{re.escape(name)}(?:\b|[.#:\[])", template_css)
            ]
            if missing_semantic:
                raise ValidationError("模板语义类缺少真实 CSS 规则：" + "、".join(missing_semantic))
            records[style_id] = TemplateRecord(
                style_id=style_id,
                aliases=aliases,
                template_id=item["template_id"],
                asset_path=path,
                template_hash=self.skill.manifest[path],
                theme_id=item["theme_id"],
                semantic_classes=semantic,
                allowed_layouts=allowed,
                body_layouts=body,
                cover_layout=item["cover_layout"],
                closing_layout=item["closing_layout"],
                layout_signatures=signatures,
            )
        if self.default_style_id not in records:
            raise ValidationError("模板注册表默认风格不存在")
        self.records = records
        self.registry_hash = self.skill.manifest[REGISTRY_PATH]

    def resolve(self, style_id: str) -> TemplateRecord:
        try:
            return self.records[style_id]
        except KeyError as exc:
            raise ValidationError("DesignContract 引用了未注册模板风格") from exc

    def select(self, task_card: dict[str, Any]) -> TemplateRecord:
        text = json.dumps(task_card, ensure_ascii=False, sort_keys=True).casefold()
        matches = [
            record for record in self.records.values()
            if record.style_id.casefold() in text or any(alias in text for alias in record.aliases)
        ]
        if len(matches) > 1:
            # Specific aliases win over a coincidental style-id substring.
            matches.sort(key=lambda item: max((len(alias) for alias in item.aliases if alias in text), default=0), reverse=True)
        return matches[0] if matches else self.records[self.default_style_id]


def build_design_contract(
    *,
    task_id: str,
    task_card: dict[str, Any],
    input_snapshot_hash: str,
    outline_hash: str,
    slide_ids: list[str],
    created_at: str,
    outline_blocks: dict[str, str] | None = None,
    registry: TemplateRegistry | None = None,
) -> dict[str, Any]:
    registry = registry or TemplateRegistry()
    if not slide_ids or len(slide_ids) != len(set(slide_ids)):
        raise ValidationError("DesignContract 页面范围无效")
    template = registry.select(task_card)
    if outline_blocks is not None and set(outline_blocks) != set(slide_ids):
        raise ValidationError("DesignContract 内容形状页面范围无效")
    contracts = []
    body_index = 0
    for index, slide_id in enumerate(slide_ids):
        if index == 0:
            layout, theme, role, recipe = template.cover_layout, "accent" if template.style_id == "swiss" else "dark", "cover", "hero"
        elif index == len(slide_ids) - 1 and len(slide_ids) > 1:
            layout, theme, role, recipe = template.closing_layout, "split" if template.style_id == "swiss" else "light", "closing", "split-statement" if template.style_id == "swiss" else "cascade"
        else:
            layout = (
                _swiss_layout_for_outline(outline_blocks.get(slide_id, ""), body_index)
                if template.style_id == "swiss" and outline_blocks is not None
                else template.body_layouts[body_index % len(template.body_layouts)]
            )
            theme = ("light", "grey", "dark")[body_index % 3] if template.style_id == "swiss" else ("light", "dark")[body_index % 2]
            role, recipe = "body", "cascade"
            body_index += 1
        contracts.append({
            "slide_id": slide_id,
            "layout_id": layout,
            "theme": theme,
            "visual_role": role,
            "animation_recipe": recipe,
            "minimum_animation_markers": 2,
        })
    seed = {
        "task_id": task_id,
        "input_snapshot_hash": input_snapshot_hash,
        "outline_hash": outline_hash,
        "style_id": template.style_id,
        "template_id": template.template_id,
        "template_hash": template.template_hash,
        "theme_id": template.theme_id,
        "semantic_classes": list(template.semantic_classes),
        "registry_version": registry.version,
        "registry_hash": registry.registry_hash,
        "allowed_layouts": list(template.allowed_layouts),
        "slide_contracts": contracts,
    }
    contract_id = f"design-{hashlib.sha256(_canonical(seed)).hexdigest()[:20]}"
    return {
        "contract_id": contract_id,
        **seed,
        "created_at": created_at,
        "schema_version": "1.0",
    }


def scope_design_contract(
    contract: dict[str, Any],
    slide_ids: list[str],
    registry: TemplateRegistry | None = None,
) -> dict[str, Any]:
    """Derive a self-consistent contract for one bounded generation batch.

    The persisted contract remains the deck-wide source of truth.  A real HTML
    builder still needs a smaller contract containing only the requested pages,
    but ``contract_id`` is content-addressed and therefore must be recomputed
    after that projection.  The separate artifact hash passed to the builder is
    intentionally left deck-wide so generated fragments remain bound to the
    persisted contract used by the final render gate.
    """
    registry = registry or TemplateRegistry()
    validate_design_contract(contract, registry)
    if not slide_ids or len(slide_ids) != len(set(slide_ids)):
        raise ValidationError("DesignContract 批次页面范围无效")
    by_id = {item["slide_id"]: item for item in contract["slide_contracts"]}
    if any(slide_id not in by_id for slide_id in slide_ids):
        raise ValidationError("DesignContract 批次包含未登记页面")
    scoped = {
        **contract,
        "slide_contracts": [dict(by_id[slide_id]) for slide_id in slide_ids],
    }
    seed = {key: scoped[key] for key in (
        "task_id", "input_snapshot_hash", "outline_hash", "style_id", "template_id",
        "template_hash", "theme_id", "semantic_classes", "registry_version", "registry_hash",
        "allowed_layouts", "slide_contracts",
    )}
    scoped["contract_id"] = f"design-{hashlib.sha256(_canonical(seed)).hexdigest()[:20]}"
    return validate_design_contract(scoped, registry)


def validate_design_contract(contract: dict[str, Any], registry: TemplateRegistry | None = None) -> dict[str, Any]:
    registry = registry or TemplateRegistry()
    required = {
        "contract_id", "task_id", "input_snapshot_hash", "outline_hash", "style_id",
        "template_id", "template_hash", "theme_id", "semantic_classes", "registry_version", "registry_hash",
        "allowed_layouts", "slide_contracts", "created_at", "schema_version",
    }
    if not isinstance(contract, dict) or set(contract) != required or contract.get("schema_version") != "1.0":
        raise ValidationError("DesignContract 结构无效")
    if (
        not isinstance(contract.get("task_id"), str) or not contract["task_id"]
        or not isinstance(contract.get("created_at"), str) or not contract["created_at"]
        or any(not isinstance(contract.get(name), str) or not re.fullmatch(r"[0-9a-f]{64}", contract[name])
               for name in ("input_snapshot_hash", "outline_hash", "template_hash", "registry_hash"))
    ):
        raise ValidationError("DesignContract 标识或哈希无效")
    template = registry.resolve(contract["style_id"])
    if (
        contract["template_id"] != template.template_id
        or contract["template_hash"] != template.template_hash
        or contract["theme_id"] != template.theme_id
        or contract["semantic_classes"] != list(template.semantic_classes)
        or contract["registry_version"] != registry.version
        or contract["registry_hash"] != registry.registry_hash
        or contract["allowed_layouts"] != list(template.allowed_layouts)
    ):
        raise ValidationError("DesignContract 与锁定模板注册表不一致")
    if not isinstance(contract["semantic_classes"], list) or len(contract["semantic_classes"]) != len(set(contract["semantic_classes"])):
        raise ValidationError("DesignContract 语义类清单无效")
    slides = contract.get("slide_contracts")
    if not isinstance(slides, list) or not slides:
        raise ValidationError("DesignContract 缺少页面契约")
    allowed = set(template.allowed_layouts)
    seen = set()
    for item in slides:
        if not isinstance(item, dict) or set(item) != {
            "slide_id", "layout_id", "theme", "visual_role", "animation_recipe", "minimum_animation_markers",
        }:
            raise ValidationError("DesignContract 页面字段无效")
        if any(not isinstance(item.get(name), str) or not _IDENTIFIER.fullmatch(item[name])
               for name in ("slide_id", "layout_id", "theme", "visual_role", "animation_recipe")):
            raise ValidationError("DesignContract 页面标识无效")
        if item["slide_id"] in seen or item["layout_id"] not in allowed:
            raise ValidationError("DesignContract 页面布局未登记或重复")
        if item["theme"] not in set(contract["semantic_classes"]):
            raise ValidationError("DesignContract 页面主题类未登记")
        if not isinstance(item["minimum_animation_markers"], int) or item["minimum_animation_markers"] < 0:
            raise ValidationError("DesignContract 动效数量无效")
        seen.add(item["slide_id"])
    seed = {key: contract[key] for key in (
        "task_id", "input_snapshot_hash", "outline_hash", "style_id", "template_id",
        "template_hash", "theme_id", "semantic_classes", "registry_version", "registry_hash",
        "allowed_layouts", "slide_contracts",
    )}
    expected_id = f"design-{hashlib.sha256(_canonical(seed)).hexdigest()[:20]}"
    if contract["contract_id"] != expected_id:
        raise ValidationError("DesignContract 内容与标识不一致")
    return contract
