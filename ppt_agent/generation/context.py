from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import ResourceRecord, TaskBrief, canonical_json, content_sha256
from .errors import ContractValidationError, ErrorContext


CONTEXT_SCHEMA_VERSION = "2.0"
CONTEXT_ID = re.compile(r"^context-[0-9a-f]{24}$")
CONTEXT_SECTION_NAMES = (
    "ORIGINAL_USER_PROMPT",
    "CLARIFICATION_TRANSCRIPT",
    "CONFIRMED_TASK_CARD",
    "SOURCE_MATERIALS_AND_ASSETS",
    "CONFIRMED_CONSTRAINTS",
    "LINEAGE",
)
STAGE_NAMES = frozenset({"brief", "narrative", "outline", "sample", "deck_batch", "deck", "modify"})


def _fail(message: str, path: str) -> None:
    raise ContractValidationError(message, context=ErrorContext(field_path=path))


def _freeze_json(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) or not key.strip() for key in value):
            _fail("对象键必须是非空字符串", path)
        return MappingProxyType({key: _freeze_json(item, f"{path}.{key}") for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{path}[{index}]") for index, item in enumerate(value))
    _fail("字段必须是 JSON 值", path)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _json_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("字段必须是对象", path)
    frozen = _freeze_json(value, path)
    try:
        canonical_json(_thaw_json(frozen))
    except (TypeError, ValueError):
        _fail("字段必须可序列化为 JSON", path)
    return frozen


@dataclass(frozen=True)
class ContextTextSource:
    source_id: str
    source_ref: str
    media_type: str
    content_hash: str
    content: str

    @classmethod
    def create(cls, source_id: str, source_ref: str, content: str, media_type: str = "text/plain; charset=utf-8") -> "ContextTextSource":
        return cls.parse({
            "source_id": source_id,
            "source_ref": source_ref,
            "media_type": media_type,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content,
        })

    @classmethod
    def parse(cls, value: Any, path: str = "context_text_source") -> "ContextTextSource":
        if not isinstance(value, Mapping):
            _fail("字段必须是对象", path)
        required = {"source_id", "source_ref", "media_type", "content_hash", "content"}
        if set(value) != required:
            _fail("字段集合无效", path)
        source_id = value["source_id"]
        source_ref = value["source_ref"]
        media_type = value["media_type"]
        content_hash = value["content_hash"]
        content = value["content"]
        if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", source_id):
            _fail("source_id 格式无效", f"{path}.source_id")
        if not isinstance(source_ref, str) or not source_ref.strip() or len(source_ref) > 1_024:
            _fail("source_ref 格式无效", f"{path}.source_ref")
        if not isinstance(media_type, str) or not media_type.strip() or len(media_type) > 128:
            _fail("media_type 格式无效", f"{path}.media_type")
        if not isinstance(content, str) or not content.strip() or len(content) > 2_000_000:
            _fail("content 必须是非空受限文本", f"{path}.content")
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if not isinstance(content_hash, str) or content_hash != expected:
            _fail("content_hash 与文本内容不一致", f"{path}.content_hash")
        return cls(source_id, source_ref, media_type, content_hash, content)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_ref": self.source_ref,
            "media_type": self.media_type,
            "content_hash": self.content_hash,
            "content": self.content,
        }


@dataclass(frozen=True)
class GenerationContextV2:
    """Content-addressed, deeply immutable input shared by every generation stage."""

    context_snapshot_id: str
    original_prompt: ContextTextSource
    clarification_transcript: tuple[Mapping[str, Any], ...]
    normalized_task_card: Mapping[str, Any]
    source_texts: tuple[ContextTextSource, ...]
    resource_manifest: tuple[ResourceRecord, ...]
    confirmed_constraints: Mapping[str, Any]
    lineage: Mapping[str, Any]
    schema_version: str = CONTEXT_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        original_prompt: ContextTextSource,
        clarification_transcript: Iterable[Mapping[str, Any]],
        normalized_task_card: Mapping[str, Any],
        source_texts: Iterable[ContextTextSource],
        resource_manifest: Iterable[ResourceRecord | Mapping[str, Any]],
        confirmed_constraints: Mapping[str, Any],
        lineage: Mapping[str, Any],
    ) -> "GenerationContextV2":
        transcript = [_thaw_json(_freeze_json(value, f"clarification_transcript[{index}]")) for index, value in enumerate(clarification_transcript)]
        sources = [value.to_dict() if isinstance(value, ContextTextSource) else value for value in source_texts]
        resources = [value.to_dict() if isinstance(value, ResourceRecord) else value for value in resource_manifest]
        seed = {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "original_prompt": original_prompt.to_dict(),
            "clarification_transcript": transcript,
            "normalized_task_card": _thaw_json(_freeze_json(normalized_task_card, "normalized_task_card")),
            "source_texts": sources,
            "resource_manifest": resources,
            "confirmed_constraints": _thaw_json(_freeze_json(confirmed_constraints, "confirmed_constraints")),
            "lineage": _thaw_json(_freeze_json(lineage, "lineage")),
        }
        return cls.parse({"context_snapshot_id": f"context-{content_sha256(seed)[:24]}", **seed})

    @classmethod
    def parse(cls, value: Any) -> "GenerationContextV2":
        if not isinstance(value, Mapping):
            _fail("GenerationContextV2 必须是对象", "generation_context")
        required = {
            "schema_version", "context_snapshot_id", "original_prompt", "clarification_transcript",
            "normalized_task_card", "source_texts", "resource_manifest", "confirmed_constraints", "lineage",
        }
        if set(value) != required:
            _fail("GenerationContextV2 字段集合无效", "generation_context")
        if value["schema_version"] != CONTEXT_SCHEMA_VERSION:
            _fail("GenerationContextV2 版本不受支持", "generation_context.schema_version")
        context_id = value["context_snapshot_id"]
        if not isinstance(context_id, str) or not CONTEXT_ID.fullmatch(context_id):
            _fail("context_snapshot_id 格式无效", "generation_context.context_snapshot_id")
        original = ContextTextSource.parse(value["original_prompt"], "generation_context.original_prompt")
        transcript_value = value["clarification_transcript"]
        if not isinstance(transcript_value, (list, tuple)) or len(transcript_value) > 100:
            _fail("clarification_transcript 必须是受限数组", "generation_context.clarification_transcript")
        transcript = tuple(_json_mapping(item, f"generation_context.clarification_transcript[{index}]") for index, item in enumerate(transcript_value))
        sources_value = value["source_texts"]
        resources_value = value["resource_manifest"]
        if not isinstance(sources_value, (list, tuple)) or len(sources_value) > 256:
            _fail("source_texts 必须是受限数组", "generation_context.source_texts")
        if not isinstance(resources_value, (list, tuple)) or len(resources_value) > 256:
            _fail("resource_manifest 必须是受限数组", "generation_context.resource_manifest")
        sources = tuple(ContextTextSource.parse(item, f"generation_context.source_texts[{index}]") for index, item in enumerate(sources_value))
        resources = tuple(ResourceRecord.parse(item, f"generation_context.resource_manifest[{index}]") for index, item in enumerate(resources_value))
        if len({item.source_id for item in sources}) != len(sources):
            _fail("source_texts 的 source_id 必须唯一", "generation_context.source_texts")
        if len({item.resource_id for item in resources}) != len(resources):
            _fail("resource_manifest 的 resource_id 必须唯一", "generation_context.resource_manifest")
        lineage = _json_mapping(value["lineage"], "generation_context.lineage")
        required_lineage = {"input_snapshot_hash", "original_input_hash", "clarification_hash", "task_card_hash", "resource_manifest_hash"}
        if not required_lineage.issubset(lineage):
            _fail("lineage 缺少必需版本", "generation_context.lineage")
        for key in required_lineage:
            item = lineage[key]
            if not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item):
                _fail("lineage 版本必须是 sha256", f"generation_context.lineage.{key}")
        normalized_task_card = _json_mapping(value["normalized_task_card"], "generation_context.normalized_task_card")
        if lineage["original_input_hash"] != original.content_hash:
            _fail("lineage.original_input_hash 与原始 Prompt 不一致", "generation_context.lineage.original_input_hash")
        if lineage["task_card_hash"] != content_sha256(_thaw_json(normalized_task_card)):
            _fail("lineage.task_card_hash 与确认任务卡不一致", "generation_context.lineage.task_card_hash")
        parsed = cls(
            context_id,
            original,
            transcript,
            normalized_task_card,
            sources,
            resources,
            _json_mapping(value["confirmed_constraints"], "generation_context.confirmed_constraints"),
            lineage,
            CONTEXT_SCHEMA_VERSION,
        )
        expected_seed = parsed.to_dict()
        expected_seed.pop("context_snapshot_id")
        if context_id != f"context-{content_sha256(expected_seed)[:24]}":
            _fail("context_snapshot_id 与快照内容不一致", "generation_context.context_snapshot_id")
        return parsed

    @classmethod
    def from_task_brief(cls, brief: TaskBrief, *, input_version: str | None = None) -> "GenerationContextV2":
        original_content = brief.text_resources[0].content if brief.text_resources else canonical_json(brief.to_dict())
        original = ContextTextSource.create("original-prompt", "compat.task_brief", original_content)
        card = {
            "goal": brief.goal,
            "audience": brief.audience,
            "topic": brief.topic,
            "constraints": {
                "slide_count": brief.slide_count,
                "language": brief.language,
                "style_preferences": brief.style_preferences,
            },
        }
        source_texts = tuple(
            ContextTextSource.create(f"source-material-{index:03d}", item.source_ref, item.content, item.media_type)
            for index, item in enumerate(brief.text_resources[1:], 1)
        )
        transcript_hash = content_sha256([])
        return cls.create(
            original_prompt=original,
            clarification_transcript=(),
            normalized_task_card=card,
            source_texts=source_texts,
            resource_manifest=brief.resource_manifest,
            confirmed_constraints=card["constraints"],
            lineage={
                "input_snapshot_hash": input_version if isinstance(input_version, str) and re.fullmatch(r"[0-9a-f]{64}", input_version) else brief.sha256,
                "original_input_hash": original.content_hash,
                "clarification_hash": transcript_hash,
                "task_card_hash": content_sha256(card),
                "resource_manifest_hash": content_sha256([item.to_dict() for item in brief.resource_manifest]),
            },
        )

    @property
    def context_hash(self) -> str:
        return content_sha256(self.to_dict())

    @property
    def section_names(self) -> tuple[str, ...]:
        return CONTEXT_SECTION_NAMES

    def provider_sections(self) -> dict[str, Any]:
        return {
            "ORIGINAL_USER_PROMPT": self.original_prompt.to_dict(),
            "CLARIFICATION_TRANSCRIPT": [_thaw_json(value) for value in self.clarification_transcript],
            "CONFIRMED_TASK_CARD": _thaw_json(self.normalized_task_card),
            "SOURCE_MATERIALS_AND_ASSETS": {
                "source_texts": [item.to_dict() for item in self.source_texts],
                "resource_manifest": [item.to_dict() for item in self.resource_manifest],
            },
            "CONFIRMED_CONSTRAINTS": _thaw_json(self.confirmed_constraints),
            "LINEAGE": _thaw_json(self.lineage),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "context_snapshot_id": self.context_snapshot_id,
            "original_prompt": self.original_prompt.to_dict(),
            "clarification_transcript": [_thaw_json(value) for value in self.clarification_transcript],
            "normalized_task_card": _thaw_json(self.normalized_task_card),
            "source_texts": [item.to_dict() for item in self.source_texts],
            "resource_manifest": [item.to_dict() for item in self.resource_manifest],
            "confirmed_constraints": _thaw_json(self.confirmed_constraints),
            "lineage": _thaw_json(self.lineage),
        }


def build_stage_payload(context: GenerationContextV2, stage: str, stage_artifacts: Mapping[str, Any]) -> dict[str, Any]:
    """Build the one payload envelope used at every model boundary."""

    if stage not in STAGE_NAMES:
        raise ValueError(f"unsupported generation stage: {stage}")
    if not isinstance(stage_artifacts, Mapping):
        raise TypeError("stage_artifacts must be a mapping")
    reserved = {
        "context_snapshot_id", "context_snapshot_hash", "generation_context", "context_sections",
        "original_prompt", "clarification_transcript", "normalized_task_card", "source_texts",
        "resource_manifest", "confirmed_constraints", "lineage", "current_stage",
    }
    overlap = reserved.intersection(stage_artifacts)
    if overlap:
        raise ValueError(f"stage_artifacts overwrite context fields: {','.join(sorted(overlap))}")
    context_value = context.to_dict()
    payload = {
        "context_snapshot_id": context.context_snapshot_id,
        "context_snapshot_hash": context.context_hash,
        "generation_context": context_value,
        "context_sections": context.provider_sections(),
        "original_prompt": context_value["original_prompt"],
        "clarification_transcript": context_value["clarification_transcript"],
        "normalized_task_card": context_value["normalized_task_card"],
        "source_texts": context_value["source_texts"],
        "resource_manifest": context_value["resource_manifest"],
        "confirmed_constraints": context_value["confirmed_constraints"],
        "lineage": context_value["lineage"],
        "current_stage": stage,
        **_thaw_json(_freeze_json(stage_artifacts, "stage_artifacts")),
    }
    canonical_json(payload)
    return payload


def stage_payload_metadata(context: GenerationContextV2, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "context_snapshot_id": context.context_snapshot_id,
        "context_snapshot_hash": context.context_hash,
        "stage_payload_hash": content_sha256(dict(payload)),
        "context_sections_read": list(context.section_names),
    }
