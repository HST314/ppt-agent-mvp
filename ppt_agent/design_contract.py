from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError


_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CANVAS = {"width": 1280, "height": 720, "aspect_ratio": "16:9"}
_RESOURCES = {
    "allowed_schemes": ["data", "resources"],
    "allowed_media_types": ["image/gif", "image/jpeg", "image/png", "image/webp"],
    "max_data_uri_bytes": 10 * 1024 * 1024,
    "require_frozen_manifest": True,
}
_SAFETY = {
    "allow_network": False,
    "allow_agent_scripts": False,
    "allow_embedded_frames": False,
    "sanitize_html": True,
}
_DELIVERY = {
    "standalone_html": True,
    "required_files": [
        "deck.html",
        "index.html",
        "manifest.json",
        "presentation-technical-contract.json",
        "result.json",
    ],
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _contract_seed(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        key: contract[key]
        for key in (
            "contract_type",
            "task_id",
            "input_snapshot_hash",
            "outline_hash",
            "canvas",
            "slide_ids",
            "resources",
            "safety",
            "delivery",
        )
    }


@dataclass(frozen=True)
class PresentationTechnicalContract:
    """Framework-owned facts needed to render and deliver a presentation.

    The contract intentionally has no style, theme, component, layout or DOM
    vocabulary.  Those choices belong to the Agent-authored ``DesignIntent``.
    Dict helpers below remain the persistence boundary so stored artifacts stay
    language-neutral and content-addressed.
    """

    contract_id: str
    task_id: str
    input_snapshot_hash: str
    outline_hash: str
    canvas: dict[str, Any]
    slide_ids: tuple[str, ...]
    resources: dict[str, Any]
    safety: dict[str, Any]
    delivery: dict[str, Any]
    created_at: str
    schema_version: str = "1.0"

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "PresentationTechnicalContract":
        normalized = validate_presentation_technical_contract(value)
        return cls(
            contract_id=normalized["contract_id"],
            task_id=normalized["task_id"],
            input_snapshot_hash=normalized["input_snapshot_hash"],
            outline_hash=normalized["outline_hash"],
            canvas=dict(normalized["canvas"]),
            slide_ids=tuple(normalized["slide_ids"]),
            resources=dict(normalized["resources"]),
            safety=dict(normalized["safety"]),
            delivery=dict(normalized["delivery"]),
            created_at=normalized["created_at"],
            schema_version=normalized["schema_version"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_type": "presentation_technical_contract",
            "task_id": self.task_id,
            "input_snapshot_hash": self.input_snapshot_hash,
            "outline_hash": self.outline_hash,
            "canvas": dict(self.canvas),
            "slide_ids": list(self.slide_ids),
            "resources": dict(self.resources),
            "safety": dict(self.safety),
            "delivery": dict(self.delivery),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DesignIntent:
    """Agent-owned visual direction carried between sample and deck stages."""

    style_summary: str
    color_strategy: str
    typography_strategy: str
    layout_principles: tuple[str, ...]
    rationale: str

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "DesignIntent":
        normalized = validate_design_intent(value)
        return cls(
            style_summary=normalized["style_summary"],
            color_strategy=normalized["color_strategy"],
            typography_strategy=normalized["typography_strategy"],
            layout_principles=tuple(normalized["layout_principles"]),
            rationale=normalized["rationale"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "style_summary": self.style_summary,
            "color_strategy": self.color_strategy,
            "typography_strategy": self.typography_strategy,
            "layout_principles": list(self.layout_principles),
            "rationale": self.rationale,
        }


def default_design_intent() -> dict[str, Any]:
    return {
        "style_summary": "清晰、克制并以内容层级为中心的演示设计",
        "color_strategy": "使用高对比的中性色与单一强调色",
        "typography_strategy": "使用系统无衬线字体并保持明确字号层级",
        "layout_principles": ["固定画布内留出安全边距", "同类信息使用一致的视觉结构"],
        "rationale": "在没有额外视觉方向时提供稳定且可读的通用基线",
    }


def validate_design_intent(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        value = default_design_intent()
    required = {
        "style_summary",
        "color_strategy",
        "typography_strategy",
        "layout_principles",
        "rationale",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValidationError("DesignIntent 结构无效")
    if any(not isinstance(value[name], str) or not value[name].strip() for name in required - {"layout_principles"}):
        raise ValidationError("DesignIntent 文本字段不得为空")
    principles = value["layout_principles"]
    if (
        not isinstance(principles, list)
        or not 1 <= len(principles) <= 12
        or any(not isinstance(item, str) or not item.strip() for item in principles)
        or len(principles) != len(set(principles))
    ):
        raise ValidationError("DesignIntent 布局原则无效")
    encoded = len(_canonical(value))
    if encoded > 32 * 1024:
        raise ValidationError("DesignIntent 超过 32 KiB 限制")
    return {
        "style_summary": value["style_summary"].strip(),
        "color_strategy": value["color_strategy"].strip(),
        "typography_strategy": value["typography_strategy"].strip(),
        "layout_principles": [item.strip() for item in principles],
        "rationale": value["rationale"].strip(),
    }


def validate_shared_design_assets(value: dict[str, Any] | None) -> dict[str, str]:
    if value is None:
        value = {"css": ""}
    if not isinstance(value, dict) or set(value) != {"css"} or not isinstance(value.get("css"), str):
        raise ValidationError("共享设计资产结构无效")
    css = value["css"].strip()
    if len(css.encode()) > 256 * 1024:
        raise ValidationError("共享样式超过 256 KiB 限制")
    return {"css": css}


def build_presentation_technical_contract(
    *,
    task_id: str,
    input_snapshot_hash: str,
    outline_hash: str,
    slide_ids: list[str],
    created_at: str,
) -> dict[str, Any]:
    contract = {
        "contract_type": "presentation_technical_contract",
        "task_id": task_id,
        "input_snapshot_hash": input_snapshot_hash,
        "outline_hash": outline_hash,
        "canvas": dict(_CANVAS),
        "slide_ids": list(slide_ids),
        "resources": {**_RESOURCES, "allowed_schemes": list(_RESOURCES["allowed_schemes"]), "allowed_media_types": list(_RESOURCES["allowed_media_types"])},
        "safety": dict(_SAFETY),
        "delivery": {**_DELIVERY, "required_files": list(_DELIVERY["required_files"])},
    }
    contract["contract_id"] = f"technical-{hashlib.sha256(_canonical(contract)).hexdigest()[:20]}"
    return validate_presentation_technical_contract({
        "contract_id": contract.pop("contract_id"),
        **contract,
        "created_at": created_at,
        "schema_version": "1.0",
    })


def scope_presentation_technical_contract(contract: dict[str, Any], slide_ids: list[str]) -> dict[str, Any]:
    validate_presentation_technical_contract(contract)
    if not slide_ids or len(slide_ids) != len(set(slide_ids)) or any(item not in contract["slide_ids"] for item in slide_ids):
        raise ValidationError("PresentationTechnicalContract 批次页面范围无效")
    scoped = {**contract, "slide_ids": list(slide_ids)}
    scoped["contract_id"] = f"technical-{hashlib.sha256(_canonical(_contract_seed(scoped))).hexdigest()[:20]}"
    return validate_presentation_technical_contract(scoped)


def validate_presentation_technical_contract(contract: dict[str, Any]) -> dict[str, Any]:
    required = {
        "contract_id",
        "contract_type",
        "task_id",
        "input_snapshot_hash",
        "outline_hash",
        "canvas",
        "slide_ids",
        "resources",
        "safety",
        "delivery",
        "created_at",
        "schema_version",
    }
    if not isinstance(contract, dict) or set(contract) != required:
        raise ValidationError("PresentationTechnicalContract 结构无效")
    if contract["contract_type"] != "presentation_technical_contract" or contract["schema_version"] != "1.0":
        raise ValidationError("PresentationTechnicalContract 类型或版本无效")
    if not isinstance(contract["task_id"], str) or not contract["task_id"] or not isinstance(contract["created_at"], str) or not contract["created_at"]:
        raise ValidationError("PresentationTechnicalContract 标识无效")
    if any(not isinstance(contract[name], str) or not _HASH.fullmatch(contract[name]) for name in ("input_snapshot_hash", "outline_hash")):
        raise ValidationError("PresentationTechnicalContract 哈希无效")
    slides = contract["slide_ids"]
    if not isinstance(slides, list) or not slides or len(slides) != len(set(slides)) or any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in slides):
        raise ValidationError("PresentationTechnicalContract 页面范围无效")
    if contract["canvas"] != _CANVAS:
        raise ValidationError("PresentationTechnicalContract 画布无效")
    if contract["resources"] != _RESOURCES:
        raise ValidationError("PresentationTechnicalContract 资源策略无效")
    if contract["safety"] != _SAFETY:
        raise ValidationError("PresentationTechnicalContract 安全策略无效")
    if contract["delivery"] != _DELIVERY:
        raise ValidationError("PresentationTechnicalContract 交付策略无效")
    expected_id = f"technical-{hashlib.sha256(_canonical(_contract_seed(contract))).hexdigest()[:20]}"
    if contract["contract_id"] != expected_id:
        raise ValidationError("PresentationTechnicalContract 内容与标识不一致")
    return contract


# Compatibility names keep persisted artifact/API callers stable while the
# value they receive is the v2 framework-owned technical contract.
build_design_contract = build_presentation_technical_contract
scope_design_contract = scope_presentation_technical_contract
validate_design_contract = validate_presentation_technical_contract
