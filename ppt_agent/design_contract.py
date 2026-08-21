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


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class TemplateRecord:
    style_id: str
    aliases: tuple[str, ...]
    template_id: str
    asset_path: str
    template_hash: str
    theme_id: str
    allowed_layouts: tuple[str, ...]
    body_layouts: tuple[str, ...]
    cover_layout: str
    closing_layout: str

    def public(self) -> dict[str, Any]:
        return {
            "style_id": self.style_id,
            "template_id": self.template_id,
            "asset_path": self.asset_path,
            "template_hash": self.template_hash,
            "theme_id": self.theme_id,
            "allowed_layouts": list(self.allowed_layouts),
            "body_layouts": list(self.body_layouts),
            "cover_layout": self.cover_layout,
            "closing_layout": self.closing_layout,
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
                "allowed_layouts", "body_layouts", "cover_layout", "closing_layout",
            }
            if set(item) != required:
                raise ValidationError("模板注册项字段无效")
            style_id = item["style_id"]
            path = item["asset_path"]
            allowed = tuple(item["allowed_layouts"])
            body = tuple(item["body_layouts"])
            values = (style_id, item["template_id"], item["theme_id"], *allowed)
            if any(not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) for value in values):
                raise ValidationError("模板注册项标识无效")
            if style_id in records or path not in self.skill.manifest or not path.startswith("assets/"):
                raise ValidationError("模板注册项重复或未锁定")
            if not allowed or len(set(allowed)) != len(allowed) or not body or not set(body).issubset(allowed):
                raise ValidationError("模板布局登记无效")
            if item["cover_layout"] not in allowed or item["closing_layout"] not in allowed:
                raise ValidationError("模板封面或封底布局未登记")
            aliases = tuple(str(alias).strip().casefold() for alias in item["aliases"] if str(alias).strip())
            if not aliases:
                raise ValidationError("模板别名不得为空")
            records[style_id] = TemplateRecord(
                style_id=style_id,
                aliases=aliases,
                template_id=item["template_id"],
                asset_path=path,
                template_hash=self.skill.manifest[path],
                theme_id=item["theme_id"],
                allowed_layouts=allowed,
                body_layouts=body,
                cover_layout=item["cover_layout"],
                closing_layout=item["closing_layout"],
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
    registry: TemplateRegistry | None = None,
) -> dict[str, Any]:
    registry = registry or TemplateRegistry()
    if not slide_ids or len(slide_ids) != len(set(slide_ids)):
        raise ValidationError("DesignContract 页面范围无效")
    template = registry.select(task_card)
    contracts = []
    body_index = 0
    for index, slide_id in enumerate(slide_ids):
        if index == 0:
            layout, theme, role, recipe = template.cover_layout, "accent" if template.style_id == "swiss" else "hero-dark", "cover", "hero"
        elif index == len(slide_ids) - 1 and len(slide_ids) > 1:
            layout, theme, role, recipe = template.closing_layout, "split" if template.style_id == "swiss" else "light", "closing", "split-statement" if template.style_id == "swiss" else "cascade"
        else:
            layout = template.body_layouts[body_index % len(template.body_layouts)]
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
        "template_hash", "theme_id", "registry_version", "registry_hash",
        "allowed_layouts", "slide_contracts",
    )}
    scoped["contract_id"] = f"design-{hashlib.sha256(_canonical(seed)).hexdigest()[:20]}"
    return validate_design_contract(scoped, registry)


def validate_design_contract(contract: dict[str, Any], registry: TemplateRegistry | None = None) -> dict[str, Any]:
    registry = registry or TemplateRegistry()
    required = {
        "contract_id", "task_id", "input_snapshot_hash", "outline_hash", "style_id",
        "template_id", "template_hash", "theme_id", "registry_version", "registry_hash",
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
        or contract["registry_version"] != registry.version
        or contract["registry_hash"] != registry.registry_hash
        or contract["allowed_layouts"] != list(template.allowed_layouts)
    ):
        raise ValidationError("DesignContract 与锁定模板注册表不一致")
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
        if not isinstance(item["minimum_animation_markers"], int) or item["minimum_animation_markers"] < 0:
            raise ValidationError("DesignContract 动效数量无效")
        seen.add(item["slide_id"])
    seed = {key: contract[key] for key in (
        "task_id", "input_snapshot_hash", "outline_hash", "style_id", "template_id",
        "template_hash", "theme_id", "registry_version", "registry_hash",
        "allowed_layouts", "slide_contracts",
    )}
    expected_id = f"design-{hashlib.sha256(_canonical(seed)).hexdigest()[:20]}"
    if contract["contract_id"] != expected_id:
        raise ValidationError("DesignContract 内容与标识不一致")
    return contract
