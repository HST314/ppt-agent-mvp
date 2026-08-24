from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, ClassVar, Iterable, Mapping, Sequence

from .errors import ContractValidationError, ErrorContext


CONTRACT_VERSION = "1.0"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
LAYOUT_FAMILIES = frozenset({"cover", "statement", "columns", "metrics", "table", "image", "quote", "closing"})
BLOCK_TYPES = frozenset({"heading", "paragraph", "bullets", "metric", "table", "image", "quote"})
MAX_VISIBLE_CHARACTERS = 420


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha256(value: Any) -> str:
    payload = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fail(message: str, path: str) -> None:
    raise ContractValidationError(message, context=ErrorContext(field_path=path))


def _object(value: Any, path: str, *, required: Iterable[str], optional: Iterable[str] = ()) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("字段必须是对象", path)
    allowed = set(required) | set(optional) | {"schema_version"}
    missing = set(required) - set(value)
    unknown = set(value) - allowed
    if missing:
        _fail(f"缺少字段：{','.join(sorted(missing))}", path)
    if unknown:
        _fail(f"包含未知字段：{','.join(sorted(unknown))}", path)
    if value.get("schema_version", CONTRACT_VERSION) != CONTRACT_VERSION:
        _fail("契约版本不受支持", f"{path}.schema_version")
    return value


def _string(value: Any, path: str, *, maximum: int = 4_000, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail("字段必须是非空字符串", path)
    normalized = value.strip() if not allow_empty else value
    if len(normalized) > maximum:
        _fail(f"字段长度超过 {maximum}", path)
    return normalized


def _identifier(value: Any, path: str) -> str:
    value = _string(value, path, maximum=128)
    if not ID_PATTERN.fullmatch(value):
        _fail("标识符格式无效", path)
    return value


def _string_list(value: Any, path: str, *, maximum_items: int = 100, maximum_length: int = 1_000, unique: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum_items:
        _fail("字段必须是受限数组", path)
    result = tuple(_string(item, f"{path}[{index}]", maximum=maximum_length) for index, item in enumerate(value))
    if unique and len(set(result)) != len(result):
        _fail("数组元素必须唯一", path)
    return result


def _json_mapping(value: Any, path: str, *, maximum_items: int = 32) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > maximum_items:
        _fail("字段必须是受限对象", path)
    try:
        canonical_json(value)
    except (TypeError, ValueError):
        _fail("字段必须是 JSON 对象", path)
    if any(not isinstance(key, str) or not key.strip() for key in value):
        _fail("对象键必须是非空字符串", path)
    return dict(value)


class Contract:
    schema_version: str
    TITLE: ClassVar[str]
    SCHEMA: ClassVar[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def parse(cls, value: Any):
        raise NotImplementedError

    @classmethod
    def provider_schema(cls) -> dict[str, Any]:
        # The configured Responses-compatible provider supports strict schema
        # structure, const, pattern and size bounds, but not ``uniqueItems``.
        # Uniqueness remains a mandatory local invariant after provider
        # validation; removing only that unsupported annotation keeps the
        # provider boundary structural and fail-closed.
        return {"name": cls.TITLE, "strict": True, "schema": _provider_schema_subset(cls.SCHEMA)}

    @property
    def sha256(self) -> str:
        return content_sha256(self.to_dict())


RESOURCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "resource_id": {"type": "string", "pattern": ID_PATTERN.pattern},
        "uri": {"type": "string", "minLength": 1, "maxLength": 2048},
        "media_type": {"type": "string", "minLength": 1, "maxLength": 128},
        "content_hash": {"type": "string", "pattern": SHA256_PATTERN.pattern},
    },
    "required": ["resource_id", "uri", "media_type", "content_hash"],
}


@dataclass(frozen=True)
class ResourceRecord:
    resource_id: str
    uri: str
    media_type: str
    content_hash: str

    @classmethod
    def parse(cls, value: Any, path: str = "resource") -> "ResourceRecord":
        item = _object(value, path, required=("resource_id", "uri", "media_type", "content_hash"))
        content_hash = _string(item["content_hash"], f"{path}.content_hash", maximum=64)
        if not SHA256_PATTERN.fullmatch(content_hash):
            _fail("资源哈希必须是 sha256", f"{path}.content_hash")
        return cls(
            _identifier(item["resource_id"], f"{path}.resource_id"),
            _string(item["uri"], f"{path}.uri", maximum=2048),
            _string(item["media_type"], f"{path}.media_type", maximum=128),
            content_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"resource_id": self.resource_id, "uri": self.uri, "media_type": self.media_type, "content_hash": self.content_hash}


@dataclass(frozen=True)
class ConfirmedFact:
    fact_id: str
    text: str
    source_refs: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any, path: str) -> "ConfirmedFact":
        item = _object(value, path, required=("fact_id", "text", "source_refs"))
        return cls(
            _identifier(item["fact_id"], f"{path}.fact_id"),
            _string(item["text"], f"{path}.text", maximum=2_000),
            _string_list(item["source_refs"], f"{path}.source_refs", maximum_items=32, maximum_length=128),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"fact_id": self.fact_id, "text": self.text, "source_refs": list(self.source_refs)}


FACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "fact_id": {"type": "string", "pattern": ID_PATTERN.pattern},
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        "source_refs": {"type": "array", "maxItems": 32, "uniqueItems": True, "items": {"type": "string", "pattern": ID_PATTERN.pattern}},
    },
    "required": ["fact_id", "text", "source_refs"],
}


@dataclass(frozen=True)
class TaskBrief(Contract):
    goal: str
    audience: str
    topic: str
    slide_count: int
    language: str
    style_preferences: dict[str, Any]
    resource_manifest: tuple[ResourceRecord, ...]
    confirmed_facts: tuple[ConfirmedFact, ...]
    schema_version: str = CONTRACT_VERSION

    TITLE = "task_brief_v1"
    SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "const": CONTRACT_VERSION},
            "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
            "audience": {"type": "string", "minLength": 1, "maxLength": 1000},
            "topic": {"type": "string", "minLength": 1, "maxLength": 1000},
            "slide_count": {"type": "integer", "minimum": 1, "maximum": 100},
            "language": {"type": "string", "minLength": 1, "maxLength": 64},
            "style_preferences": {"type": "object", "maxProperties": 32},
            "resource_manifest": {"type": "array", "maxItems": 256, "items": RESOURCE_SCHEMA},
            "confirmed_facts": {"type": "array", "maxItems": 256, "items": FACT_SCHEMA},
        },
        "required": ["schema_version", "goal", "audience", "topic", "slide_count", "language", "style_preferences", "resource_manifest", "confirmed_facts"],
    }

    @classmethod
    def parse(cls, value: Any) -> "TaskBrief":
        item = _object(value, "task_brief", required=("goal", "audience", "topic", "slide_count", "language", "style_preferences", "resource_manifest", "confirmed_facts"))
        count = item["slide_count"]
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            _fail("slide_count 必须在 1 到 100 之间", "task_brief.slide_count")
        resources_value = item["resource_manifest"]
        facts_value = item["confirmed_facts"]
        if not isinstance(resources_value, (list, tuple)) or len(resources_value) > 256:
            _fail("resource_manifest 必须是受限数组", "task_brief.resource_manifest")
        if not isinstance(facts_value, (list, tuple)) or len(facts_value) > 256:
            _fail("confirmed_facts 必须是受限数组", "task_brief.confirmed_facts")
        resources = tuple(ResourceRecord.parse(value, f"task_brief.resource_manifest[{index}]") for index, value in enumerate(resources_value))
        facts = tuple(ConfirmedFact.parse(value, f"task_brief.confirmed_facts[{index}]") for index, value in enumerate(facts_value))
        if len({value.resource_id for value in resources}) != len(resources):
            _fail("资源 ID 必须唯一", "task_brief.resource_manifest")
        if len({value.fact_id for value in facts}) != len(facts):
            _fail("事实 ID 必须唯一", "task_brief.confirmed_facts")
        known_resources = {value.resource_id for value in resources}
        if any(not set(value.source_refs).issubset(known_resources) for value in facts):
            _fail("事实引用了未知资源", "task_brief.confirmed_facts")
        return cls(
            _string(item["goal"], "task_brief.goal", maximum=2_000),
            _string(item["audience"], "task_brief.audience", maximum=1_000),
            _string(item["topic"], "task_brief.topic", maximum=1_000),
            count,
            _string(item["language"], "task_brief.language", maximum=64),
            _json_mapping(item["style_preferences"], "task_brief.style_preferences"),
            resources,
            facts,
            item.get("schema_version", CONTRACT_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "audience": self.audience,
            "topic": self.topic,
            "slide_count": self.slide_count,
            "language": self.language,
            "style_preferences": self.style_preferences,
            "resource_manifest": [value.to_dict() for value in self.resource_manifest],
            "confirmed_facts": [value.to_dict() for value in self.confirmed_facts],
        }


STORY_BEAT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "beat_id": {"type": "string", "pattern": ID_PATTERN.pattern},
        "purpose": {"type": "string", "minLength": 1, "maxLength": 400},
        "message": {"type": "string", "minLength": 1, "maxLength": 1200},
    },
    "required": ["beat_id", "purpose", "message"],
}


@dataclass(frozen=True)
class StoryBeat:
    beat_id: str
    purpose: str
    message: str

    @classmethod
    def parse(cls, value: Any, path: str) -> "StoryBeat":
        item = _object(value, path, required=("beat_id", "purpose", "message"))
        return cls(_identifier(item["beat_id"], f"{path}.beat_id"), _string(item["purpose"], f"{path}.purpose", maximum=400), _string(item["message"], f"{path}.message", maximum=1_200))

    def to_dict(self) -> dict[str, Any]:
        return {"beat_id": self.beat_id, "purpose": self.purpose, "message": self.message}


@dataclass(frozen=True)
class NarrativeSpec(Contract):
    thesis: str
    audience_takeaway: str
    story_arc: tuple[StoryBeat, ...]
    evidence_refs: tuple[str, ...]
    tone: str
    schema_version: str = CONTRACT_VERSION

    TITLE = "narrative_spec_v1"
    SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "const": CONTRACT_VERSION},
            "thesis": {"type": "string", "minLength": 1, "maxLength": 2000},
            "audience_takeaway": {"type": "string", "minLength": 1, "maxLength": 2000},
            "story_arc": {"type": "array", "minItems": 2, "maxItems": 24, "items": STORY_BEAT_SCHEMA},
            "evidence_refs": {"type": "array", "maxItems": 256, "uniqueItems": True, "items": {"type": "string", "pattern": ID_PATTERN.pattern}},
            "tone": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "required": ["schema_version", "thesis", "audience_takeaway", "story_arc", "evidence_refs", "tone"],
    }

    @classmethod
    def parse(cls, value: Any) -> "NarrativeSpec":
        item = _object(value, "narrative", required=("thesis", "audience_takeaway", "story_arc", "evidence_refs", "tone"))
        arc_value = item["story_arc"]
        if not isinstance(arc_value, (list, tuple)) or not 2 <= len(arc_value) <= 24:
            _fail("story_arc 必须包含 2 到 24 个叙事节点", "narrative.story_arc")
        arc = tuple(StoryBeat.parse(value, f"narrative.story_arc[{index}]") for index, value in enumerate(arc_value))
        if len({value.beat_id for value in arc}) != len(arc):
            _fail("叙事节点 ID 必须唯一", "narrative.story_arc")
        return cls(
            _string(item["thesis"], "narrative.thesis", maximum=2_000),
            _string(item["audience_takeaway"], "narrative.audience_takeaway", maximum=2_000),
            arc,
            _string_list(item["evidence_refs"], "narrative.evidence_refs", maximum_items=256, maximum_length=128),
            _string(item["tone"], "narrative.tone", maximum=200),
            item.get("schema_version", CONTRACT_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "thesis": self.thesis, "audience_takeaway": self.audience_takeaway, "story_arc": [value.to_dict() for value in self.story_arc], "evidence_refs": list(self.evidence_refs), "tone": self.tone}


OUTLINE_DRAFT_SLIDE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "role": {"type": "string", "minLength": 1, "maxLength": 128},
        "title": {"type": "string", "minLength": 1, "maxLength": 180},
        "message": {"type": "string", "minLength": 1, "maxLength": 1200},
        "evidence_refs": {"type": "array", "maxItems": 64, "uniqueItems": True, "items": {"type": "string", "pattern": ID_PATTERN.pattern}},
        "visual_intent": {"type": "string", "minLength": 1, "maxLength": 600},
    },
    "required": ["role", "title", "message", "evidence_refs", "visual_intent"],
}


@dataclass(frozen=True)
class OutlineSlide:
    slide_id: str
    role: str
    title: str
    message: str
    evidence_refs: tuple[str, ...]
    visual_intent: str

    @classmethod
    def parse(cls, value: Any, path: str, *, require_id: bool = True, slide_id: str | None = None) -> "OutlineSlide":
        required = ("slide_id", "role", "title", "message", "evidence_refs", "visual_intent") if require_id else ("role", "title", "message", "evidence_refs", "visual_intent")
        item = _object(value, path, required=required)
        return cls(
            _identifier(item["slide_id"] if require_id else slide_id, f"{path}.slide_id"),
            _string(item["role"], f"{path}.role", maximum=128),
            _string(item["title"], f"{path}.title", maximum=180),
            _string(item["message"], f"{path}.message", maximum=1_200),
            _string_list(item["evidence_refs"], f"{path}.evidence_refs", maximum_items=64, maximum_length=128),
            _string(item["visual_intent"], f"{path}.visual_intent", maximum=600),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"slide_id": self.slide_id, "role": self.role, "title": self.title, "message": self.message, "evidence_refs": list(self.evidence_refs), "visual_intent": self.visual_intent}


@dataclass(frozen=True)
class OutlineDraft(Contract):
    slides: tuple[OutlineSlide, ...]
    schema_version: str = CONTRACT_VERSION

    TITLE = "outline_draft_v1"
    SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "const": CONTRACT_VERSION},
            "slides": {"type": "array", "minItems": 1, "maxItems": 100, "items": OUTLINE_DRAFT_SLIDE_SCHEMA},
        },
        "required": ["schema_version", "slides"],
    }

    @classmethod
    def parse(cls, value: Any) -> "OutlineDraft":
        item = _object(value, "outline_draft", required=("slides",))
        slides_value = item["slides"]
        if not isinstance(slides_value, (list, tuple)) or not 1 <= len(slides_value) <= 100:
            _fail("slides 必须包含 1 到 100 页", "outline_draft.slides")
        slides = tuple(OutlineSlide.parse(value, f"outline_draft.slides[{index}]", require_id=False, slide_id=f"slide-{index + 1:03d}") for index, value in enumerate(slides_value))
        return cls(slides, item.get("schema_version", CONTRACT_VERSION))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "slides": [{key: value for key, value in slide.to_dict().items() if key != "slide_id"} for slide in self.slides]}


OUTLINE_SLIDE_SCHEMA = {**OUTLINE_DRAFT_SLIDE_SCHEMA, "properties": {"slide_id": {"type": "string", "pattern": ID_PATTERN.pattern}, **OUTLINE_DRAFT_SLIDE_SCHEMA["properties"]}, "required": ["slide_id", *OUTLINE_DRAFT_SLIDE_SCHEMA["required"]]}


@dataclass(frozen=True)
class OutlineSpec(Contract):
    slides: tuple[OutlineSlide, ...]
    schema_version: str = CONTRACT_VERSION

    TITLE = "outline_spec_v1"
    SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"schema_version": {"type": "string", "const": CONTRACT_VERSION}, "slides": {"type": "array", "minItems": 1, "maxItems": 100, "items": OUTLINE_SLIDE_SCHEMA}},
        "required": ["schema_version", "slides"],
    }

    @classmethod
    def parse(cls, value: Any, *, expected_slide_count: int | None = None) -> "OutlineSpec":
        item = _object(value, "outline", required=("slides",))
        slides_value = item["slides"]
        if not isinstance(slides_value, (list, tuple)) or not 1 <= len(slides_value) <= 100:
            _fail("slides 必须包含 1 到 100 页", "outline.slides")
        if expected_slide_count is not None and len(slides_value) != expected_slide_count:
            _fail("页面数与任务约束不一致", "outline.slides")
        slides = tuple(OutlineSlide.parse(value, f"outline.slides[{index}]") for index, value in enumerate(slides_value))
        identifiers = [value.slide_id for value in slides]
        if len(set(identifiers)) != len(identifiers):
            _fail("slide_id 必须唯一", "outline.slides")
        expected_ids = [f"slide-{index + 1:03d}" for index in range(len(slides))]
        if identifiers != expected_ids:
            _fail("slide_id 必须使用服务端连续规范", "outline.slides")
        return cls(slides, item.get("schema_version", CONTRACT_VERSION))

    @classmethod
    def from_draft(cls, draft: OutlineDraft, *, expected_slide_count: int) -> "OutlineSpec":
        if len(draft.slides) != expected_slide_count:
            _fail("页面数与任务约束不一致", "outline.slides")
        return cls(tuple(draft.slides), CONTRACT_VERSION)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "slides": [value.to_dict() for value in self.slides]}


BLOCK_SCHEMAS: dict[str, dict[str, Any]] = {
    "heading": {"text": {"type": "string", "minLength": 1, "maxLength": 72}},
    "paragraph": {"text": {"type": "string", "minLength": 1, "maxLength": 360}},
    "bullets": {"items": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string", "minLength": 1, "maxLength": 72}}},
    "metric": {"label": {"type": "string", "minLength": 1, "maxLength": 64}, "value": {"type": "string", "minLength": 1, "maxLength": 48}},
    "table": {"rows": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "maxLength": 48}}}},
    "image": {"asset_ref": {"type": "string", "pattern": ID_PATTERN.pattern}, "alt": {"type": "string", "minLength": 1, "maxLength": 160}},
    "quote": {"text": {"type": "string", "minLength": 1, "maxLength": 280}, "attribution": {"type": "string", "minLength": 1, "maxLength": 80}},
}


def _block_one_of() -> list[dict[str, Any]]:
    values = []
    for kind, properties in BLOCK_SCHEMAS.items():
        values.append({
            "type": "object",
            "additionalProperties": False,
            "properties": {"type": {"type": "string", "const": kind}, "block_id": {"type": "string", "pattern": ID_PATTERN.pattern}, **properties},
            "required": ["type", "block_id", *properties],
        })
    return values


CONTENT_BLOCK_SCHEMA = {"anyOf": _block_one_of()}


@dataclass(frozen=True)
class ContentBlock:
    type: str
    block_id: str
    payload: dict[str, Any]

    @classmethod
    def parse(cls, value: Any, path: str) -> "ContentBlock":
        if not isinstance(value, dict):
            _fail("内容块必须是对象", path)
        kind = value.get("type")
        if kind not in BLOCK_TYPES:
            _fail("内容块类型无效", f"{path}.type")
        fields = tuple(BLOCK_SCHEMAS[kind])
        item = _object(value, path, required=("type", "block_id", *fields))
        payload: dict[str, Any] = {}
        if kind in {"heading", "paragraph"}:
            payload["text"] = _string(item["text"], f"{path}.text", maximum=72 if kind == "heading" else 360)
        elif kind == "bullets":
            values = _string_list(item["items"], f"{path}.items", maximum_items=5, maximum_length=72, unique=False)
            if not values:
                _fail("列表不能为空", f"{path}.items")
            payload["items"] = list(values)
        elif kind == "metric":
            payload = {"label": _string(item["label"], f"{path}.label", maximum=64), "value": _string(item["value"], f"{path}.value", maximum=48)}
        elif kind == "table":
            rows = item["rows"]
            if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 6:
                _fail("表格行数无效", f"{path}.rows")
            parsed_rows = [_string_list(row, f"{path}.rows[{index}]", maximum_items=4, maximum_length=48, unique=False) for index, row in enumerate(rows)]
            if any(not row for row in parsed_rows) or len({len(row) for row in parsed_rows}) != 1:
                _fail("表格必须是非空矩形", f"{path}.rows")
            payload["rows"] = [list(row) for row in parsed_rows]
        elif kind == "image":
            payload = {"asset_ref": _identifier(item["asset_ref"], f"{path}.asset_ref"), "alt": _string(item["alt"], f"{path}.alt", maximum=160)}
        elif kind == "quote":
            payload = {"text": _string(item["text"], f"{path}.text", maximum=280), "attribution": _string(item["attribution"], f"{path}.attribution", maximum=80)}
        return cls(kind, _identifier(item["block_id"], f"{path}.block_id"), payload)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "block_id": self.block_id, **self.payload}


SLIDE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "slide_id": {"type": "string", "pattern": ID_PATTERN.pattern},
        "role": {"type": "string", "minLength": 1, "maxLength": 128},
        "title": {"type": "string", "minLength": 1, "maxLength": 72},
        "content_blocks": {"type": "array", "minItems": 1, "maxItems": 4, "items": CONTENT_BLOCK_SCHEMA},
        "layout_family": {"type": "string", "enum": sorted(LAYOUT_FAMILIES)},
        "asset_refs": {"type": "array", "maxItems": 32, "uniqueItems": True, "items": {"type": "string", "pattern": ID_PATTERN.pattern}},
        "speaker_notes": {"type": "string", "maxLength": 4000},
    },
    "required": ["slide_id", "role", "title", "content_blocks", "layout_family", "asset_refs", "speaker_notes"],
}


@dataclass(frozen=True)
class SlideSpec(Contract):
    slide_id: str
    role: str
    title: str
    content_blocks: tuple[ContentBlock, ...]
    layout_family: str
    asset_refs: tuple[str, ...]
    speaker_notes: str
    schema_version: str = CONTRACT_VERSION

    TITLE = "slide_spec_v1"
    SCHEMA = {**SLIDE_SCHEMA, "properties": {"schema_version": {"type": "string", "const": CONTRACT_VERSION}, **SLIDE_SCHEMA["properties"]}, "required": ["schema_version", *SLIDE_SCHEMA["required"]]}

    @classmethod
    def parse(cls, value: Any, path: str = "slide") -> "SlideSpec":
        item = _object(value, path, required=("slide_id", "role", "title", "content_blocks", "layout_family", "asset_refs", "speaker_notes"))
        blocks_value = item["content_blocks"]
        if not isinstance(blocks_value, (list, tuple)) or not 1 <= len(blocks_value) <= 4:
            _fail("content_blocks 必须包含 1 到 4 项", f"{path}.content_blocks")
        blocks = tuple(ContentBlock.parse(value, f"{path}.content_blocks[{index}]") for index, value in enumerate(blocks_value))
        if len({value.block_id for value in blocks}) != len(blocks):
            _fail("block_id 必须唯一", f"{path}.content_blocks")
        layout = item["layout_family"]
        if layout not in LAYOUT_FAMILIES:
            _fail("layout_family 不受支持", f"{path}.layout_family")
        title = _string(item["title"], f"{path}.title", maximum=72)
        visible_characters = len(title) + sum(_visible_characters(block) for block in blocks)
        if visible_characters > MAX_VISIBLE_CHARACTERS:
            _fail("页面可见文字超过画布容量", f"{path}.content_blocks")
        asset_refs = _string_list(item["asset_refs"], f"{path}.asset_refs", maximum_items=32, maximum_length=128)
        embedded = {block.payload["asset_ref"] for block in blocks if block.type == "image"}
        if embedded != set(asset_refs):
            _fail("asset_refs 必须与 image 内容块精确一致", f"{path}.asset_refs")
        return cls(
            _identifier(item["slide_id"], f"{path}.slide_id"),
            _string(item["role"], f"{path}.role", maximum=128),
            title,
            blocks,
            layout,
            asset_refs,
            _string(item["speaker_notes"], f"{path}.speaker_notes", maximum=4_000, allow_empty=True),
            item.get("schema_version", CONTRACT_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "slide_id": self.slide_id, "role": self.role, "title": self.title, "content_blocks": [value.to_dict() for value in self.content_blocks], "layout_family": self.layout_family, "asset_refs": list(self.asset_refs), "speaker_notes": self.speaker_notes}


def _visible_characters(block: ContentBlock) -> int:
    payload = block.payload
    if block.type in {"heading", "paragraph"}:
        return len(payload["text"])
    if block.type == "bullets":
        return sum(len(item) for item in payload["items"])
    if block.type == "metric":
        return len(payload["label"]) + len(payload["value"])
    if block.type == "table":
        return sum(len(cell) for row in payload["rows"] for cell in row)
    if block.type == "image":
        return len(payload["alt"])
    if block.type == "quote":
        return len(payload["text"]) + len(payload["attribution"])
    raise AssertionError(f"unhandled block type: {block.type}")


THEME_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "background": {"type": "string", "pattern": COLOR_PATTERN.pattern},
        "surface": {"type": "string", "pattern": COLOR_PATTERN.pattern},
        "text": {"type": "string", "pattern": COLOR_PATTERN.pattern},
        "muted_text": {"type": "string", "pattern": COLOR_PATTERN.pattern},
        "primary": {"type": "string", "pattern": COLOR_PATTERN.pattern},
        "accent": {"type": "string", "pattern": COLOR_PATTERN.pattern},
        "font_heading": {"type": "string", "minLength": 1, "maxLength": 128},
        "font_body": {"type": "string", "minLength": 1, "maxLength": 128},
        "border_radius": {"type": "integer", "minimum": 0, "maximum": 64},
        "space_unit": {"type": "integer", "minimum": 4, "maximum": 20},
    },
    "required": ["background", "surface", "text", "muted_text", "primary", "accent", "font_heading", "font_body", "border_radius", "space_unit"],
}


@dataclass(frozen=True)
class ThemeTokens(Contract):
    background: str
    surface: str
    text: str
    muted_text: str
    primary: str
    accent: str
    font_heading: str
    font_body: str
    border_radius: int
    space_unit: int
    schema_version: str = CONTRACT_VERSION

    TITLE = "theme_tokens_v1"
    SCHEMA = {**THEME_SCHEMA, "properties": {"schema_version": {"type": "string", "const": CONTRACT_VERSION}, **THEME_SCHEMA["properties"]}, "required": ["schema_version", *THEME_SCHEMA["required"]]}

    @classmethod
    def parse(cls, value: Any) -> "ThemeTokens":
        item = _object(value, "theme", required=tuple(THEME_SCHEMA["required"]))
        colors = []
        for name in ("background", "surface", "text", "muted_text", "primary", "accent"):
            color = _string(item[name], f"theme.{name}", maximum=7)
            if not COLOR_PATTERN.fullmatch(color):
                _fail("颜色必须使用六位十六进制格式", f"theme.{name}")
            colors.append(color.upper())
        radius, space = item["border_radius"], item["space_unit"]
        if isinstance(radius, bool) or not isinstance(radius, int) or not 0 <= radius <= 64:
            _fail("border_radius 范围无效", "theme.border_radius")
        if isinstance(space, bool) or not isinstance(space, int) or not 4 <= space <= 20:
            _fail("space_unit 范围无效", "theme.space_unit")
        return cls(*colors, _string(item["font_heading"], "theme.font_heading", maximum=128), _string(item["font_body"], "theme.font_body", maximum=128), radius, space, item.get("schema_version", CONTRACT_VERSION))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "background": self.background, "surface": self.surface, "text": self.text, "muted_text": self.muted_text, "primary": self.primary, "accent": self.accent, "font_heading": self.font_heading, "font_body": self.font_body, "border_radius": self.border_radius, "space_unit": self.space_unit}


@dataclass(frozen=True)
class SampleSpec(Contract):
    slides: tuple[SlideSpec, ...]
    theme_tokens: ThemeTokens
    shared_assets: tuple[str, ...]
    outline_checkpoint_id: str
    schema_version: str = CONTRACT_VERSION

    TITLE = "sample_spec_v1"
    SCHEMA = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "const": CONTRACT_VERSION},
            "slides": {"type": "array", "minItems": 2, "maxItems": 3, "items": SLIDE_SCHEMA},
            "theme_tokens": THEME_SCHEMA,
            "shared_assets": {"type": "array", "maxItems": 64, "uniqueItems": True, "items": {"type": "string", "pattern": ID_PATTERN.pattern}},
            "outline_checkpoint_id": {"type": "string", "pattern": ID_PATTERN.pattern},
        },
        "required": ["schema_version", "slides", "theme_tokens", "shared_assets", "outline_checkpoint_id"],
    }

    @classmethod
    def parse(cls, value: Any) -> "SampleSpec":
        item = _object(value, "sample", required=("slides", "theme_tokens", "shared_assets", "outline_checkpoint_id"))
        slides_value = item["slides"]
        if not isinstance(slides_value, (list, tuple)) or not 2 <= len(slides_value) <= 3:
            _fail("样品必须包含 2 到 3 页", "sample.slides")
        slides = tuple(SlideSpec.parse(value, f"sample.slides[{index}]") for index, value in enumerate(slides_value))
        _validate_slide_set(slides, "sample.slides")
        shared = _string_list(item["shared_assets"], "sample.shared_assets", maximum_items=64, maximum_length=128)
        if not {ref for slide in slides for ref in slide.asset_refs}.issubset(set(shared)):
            _fail("样品资源引用未形成闭包", "sample.shared_assets")
        return cls(slides, ThemeTokens.parse(item["theme_tokens"]), shared, _identifier(item["outline_checkpoint_id"], "sample.outline_checkpoint_id"), item.get("schema_version", CONTRACT_VERSION))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "slides": [_slide_provider_dict(value) for value in self.slides], "theme_tokens": _theme_provider_dict(self.theme_tokens), "shared_assets": list(self.shared_assets), "outline_checkpoint_id": self.outline_checkpoint_id}


@dataclass(frozen=True)
class SlideBatchSpec(Contract):
    slides: tuple[SlideSpec, ...]
    schema_version: str = CONTRACT_VERSION

    TITLE = "slide_batch_spec_v1"
    SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"schema_version": {"type": "string", "const": CONTRACT_VERSION}, "slides": {"type": "array", "minItems": 1, "maxItems": 8, "items": SLIDE_SCHEMA}}, "required": ["schema_version", "slides"]}

    @classmethod
    def parse(cls, value: Any) -> "SlideBatchSpec":
        item = _object(value, "slide_batch", required=("slides",))
        slides_value = item["slides"]
        if not isinstance(slides_value, (list, tuple)) or not 1 <= len(slides_value) <= 8:
            _fail("批次必须包含 1 到 8 页", "slide_batch.slides")
        slides = tuple(SlideSpec.parse(value, f"slide_batch.slides[{index}]") for index, value in enumerate(slides_value))
        _validate_slide_set(slides, "slide_batch.slides")
        return cls(slides, item.get("schema_version", CONTRACT_VERSION))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "slides": [_slide_provider_dict(value) for value in self.slides]}


def _bind_slide_assets(schema: dict[str, Any], allowed_assets: Sequence[str]) -> dict[str, Any]:
    bound = copy.deepcopy(schema)
    slide_schema = bound["properties"]["slides"]["items"]
    asset_refs = slide_schema["properties"]["asset_refs"]
    asset_refs["maxItems"] = len(allowed_assets)
    variants = slide_schema["properties"]["content_blocks"]["items"]["anyOf"]
    image = next((item for item in variants if item["properties"]["type"].get("const") == "image"), None)
    if allowed_assets:
        constraint = {"type": "string", "enum": list(allowed_assets)}
        asset_refs["items"] = constraint
        image["properties"]["asset_ref"] = constraint
    else:
        variants[:] = [item for item in variants if item["properties"]["type"].get("const") != "image"]
    return bound


@lru_cache(maxsize=128)
def sample_contract_for_assets(allowed_assets: tuple[str, ...], slide_count: int | None = None) -> type[SampleSpec]:
    allowed = tuple(dict.fromkeys(allowed_assets))
    schema = _bind_slide_assets(SampleSpec.SCHEMA, allowed)
    if slide_count is not None:
        schema["properties"]["slides"].update({"minItems": slide_count, "maxItems": slide_count})
    schema["properties"]["shared_assets"]["maxItems"] = len(allowed)
    if allowed:
        schema["properties"]["shared_assets"]["items"] = {"type": "string", "enum": list(allowed)}

    class BoundSampleSpec(SampleSpec):
        SCHEMA = schema

    return BoundSampleSpec


@lru_cache(maxsize=128)
def slide_batch_contract_for_assets(allowed_assets: tuple[str, ...], slide_count: int | None = None, allowed_layouts: tuple[str, ...] = ()) -> type[SlideBatchSpec]:
    schema = _bind_slide_assets(SlideBatchSpec.SCHEMA, tuple(dict.fromkeys(allowed_assets)))
    if slide_count is not None:
        schema["properties"]["slides"].update({"minItems": slide_count, "maxItems": slide_count})
    if allowed_layouts:
        schema["properties"]["slides"]["items"]["properties"]["layout_family"]["enum"] = sorted(set(allowed_layouts))

    class BoundSlideBatchSpec(SlideBatchSpec):
        SCHEMA = schema

    return BoundSlideBatchSpec


@lru_cache(maxsize=128)
def narrative_contract_for_evidence(allowed_evidence: tuple[str, ...]) -> type[NarrativeSpec]:
    allowed = tuple(dict.fromkeys(allowed_evidence))
    schema = copy.deepcopy(NarrativeSpec.SCHEMA)
    schema["properties"]["evidence_refs"]["maxItems"] = len(allowed)
    if allowed:
        schema["properties"]["evidence_refs"]["items"] = {"type": "string", "enum": list(allowed)}

    class BoundNarrativeSpec(NarrativeSpec):
        SCHEMA = schema

    return BoundNarrativeSpec


@lru_cache(maxsize=128)
def outline_contract_for_evidence(allowed_evidence: tuple[str, ...], slide_count: int) -> type[OutlineDraft]:
    allowed = tuple(dict.fromkeys(allowed_evidence))
    schema = copy.deepcopy(OutlineDraft.SCHEMA)
    schema["properties"]["slides"].update({"minItems": slide_count, "maxItems": slide_count})
    evidence = schema["properties"]["slides"]["items"]["properties"]["evidence_refs"]
    evidence["maxItems"] = len(allowed)
    if allowed:
        evidence["items"] = {"type": "string", "enum": list(allowed)}

    class BoundOutlineDraft(OutlineDraft):
        SCHEMA = schema

    return BoundOutlineDraft


@dataclass(frozen=True)
class DeckSpec(Contract):
    slides: tuple[SlideSpec, ...]
    theme_tokens: ThemeTokens
    shared_assets: tuple[str, ...]
    outline_checkpoint_id: str
    sample_checkpoint_id: str
    schema_version: str = CONTRACT_VERSION

    TITLE = "deck_spec_v1"
    SCHEMA = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "string", "const": CONTRACT_VERSION},
            "slides": {"type": "array", "minItems": 1, "maxItems": 100, "items": SLIDE_SCHEMA},
            "theme_tokens": THEME_SCHEMA,
            "shared_assets": {"type": "array", "maxItems": 256, "uniqueItems": True, "items": {"type": "string", "pattern": ID_PATTERN.pattern}},
            "outline_checkpoint_id": {"type": "string", "pattern": ID_PATTERN.pattern},
            "sample_checkpoint_id": {"type": "string", "pattern": ID_PATTERN.pattern},
        },
        "required": ["schema_version", "slides", "theme_tokens", "shared_assets", "outline_checkpoint_id", "sample_checkpoint_id"],
    }

    @classmethod
    def parse(cls, value: Any, *, expected_slide_ids: Sequence[str] | None = None, frozen_theme: ThemeTokens | None = None, allowed_layouts: set[str] | None = None) -> "DeckSpec":
        item = _object(value, "deck", required=("slides", "theme_tokens", "shared_assets", "outline_checkpoint_id", "sample_checkpoint_id"))
        slides_value = item["slides"]
        if not isinstance(slides_value, (list, tuple)) or not 1 <= len(slides_value) <= 100:
            _fail("全稿必须包含 1 到 100 页", "deck.slides")
        slides = tuple(SlideSpec.parse(value, f"deck.slides[{index}]") for index, value in enumerate(slides_value))
        _validate_slide_set(slides, "deck.slides")
        if expected_slide_ids is not None and [value.slide_id for value in slides] != list(expected_slide_ids):
            _fail("全稿页面集合或顺序与大纲不一致", "deck.slides")
        theme = ThemeTokens.parse(item["theme_tokens"])
        if frozen_theme is not None and theme != frozen_theme:
            _fail("全稿主题与已确认样品不一致", "deck.theme_tokens")
        if allowed_layouts is not None and any(value.layout_family not in allowed_layouts for value in slides):
            _fail("全稿使用了未冻结的版式族", "deck.slides")
        shared = _string_list(item["shared_assets"], "deck.shared_assets", maximum_items=256, maximum_length=128)
        if {ref for slide in slides for ref in slide.asset_refs} != set(shared):
            _fail("全稿资源清单必须与页面引用精确闭包", "deck.shared_assets")
        return cls(slides, theme, shared, _identifier(item["outline_checkpoint_id"], "deck.outline_checkpoint_id"), _identifier(item["sample_checkpoint_id"], "deck.sample_checkpoint_id"), item.get("schema_version", CONTRACT_VERSION))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "slides": [_slide_provider_dict(value) for value in self.slides], "theme_tokens": _theme_provider_dict(self.theme_tokens), "shared_assets": list(self.shared_assets), "outline_checkpoint_id": self.outline_checkpoint_id, "sample_checkpoint_id": self.sample_checkpoint_id}


def _validate_slide_set(slides: Sequence[SlideSpec], path: str) -> None:
    identifiers = [value.slide_id for value in slides]
    if len(set(identifiers)) != len(identifiers):
        _fail("slide_id 必须唯一", path)


def _provider_schema_subset(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _provider_schema_subset(item) for key, item in value.items() if key != "uniqueItems"}
    if isinstance(value, list):
        return [_provider_schema_subset(item) for item in value]
    return value


def _slide_provider_dict(value: SlideSpec) -> dict[str, Any]:
    return {key: item for key, item in value.to_dict().items() if key != "schema_version"}


def _theme_provider_dict(value: ThemeTokens) -> dict[str, Any]:
    return {key: item for key, item in value.to_dict().items() if key != "schema_version"}


def verify_evidence_refs(contract: NarrativeSpec | OutlineSpec, brief: TaskBrief) -> None:
    allowed = {value.resource_id for value in brief.resource_manifest} | {value.fact_id for value in brief.confirmed_facts}
    references = contract.evidence_refs if isinstance(contract, NarrativeSpec) else tuple(ref for slide in contract.slides for ref in slide.evidence_refs)
    unknown = sorted(set(references) - allowed)
    if unknown:
        _fail(f"引用了未知证据：{','.join(unknown)}", "evidence_refs")


def validate_slide_outline_alignment(slides: Sequence[SlideSpec], outline: OutlineSpec) -> None:
    expected = {value.slide_id: value for value in outline.slides}
    for index, slide in enumerate(slides):
        target = expected.get(slide.slide_id)
        if target is None:
            _fail("页面不在已冻结大纲中", f"slides[{index}].slide_id")
        if slide.role != target.role:
            _fail("页面角色与已冻结大纲不一致", f"slides[{index}].role")


def ensure_json_round_trip(contract: Contract) -> None:
    parsed = type(contract).parse(json.loads(canonical_json(contract.to_dict())))
    if parsed != contract:
        _fail("契约无法稳定往返", contract.TITLE)
