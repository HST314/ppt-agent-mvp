from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any, ClassVar

from .errors import ValidationError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StrictModel:
    schema_version: str = "1.0"
    KIND: ClassVar[str] = "model"

    @classmethod
    def parse(cls, value: dict[str, Any]):
        if not isinstance(value, dict):
            raise ValidationError(f"{cls.__name__} 必须是对象")
        allowed = {f.name for f in fields(cls)}
        unknown = set(value) - allowed
        if unknown:
            raise ValidationError(f"{cls.__name__} 包含未知字段：{', '.join(sorted(unknown))}")
        try:
            return cls(**value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{cls.__name__} 格式无效") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        required = [f.name for f in fields(cls) if f.default is f.default_factory]
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": cls.__name__,
                "type": "object", "additionalProperties": False,
                "properties": {f.name: {"title": f.name} for f in fields(cls)}, "required": required}


@dataclass(frozen=True)
class TaskCard(StrictModel):
    task_id: str = ""
    goal: str = ""
    audience: str = ""
    topic: str = ""
    source_format: str = "json"

    def __post_init__(self):
        if not self.task_id or not self.goal or not self.audience or not self.topic:
            raise ValidationError("任务卡必须包含 task_id、goal、audience 和 topic")
        if self.source_format not in {"json", "markdown"}:
            raise ValidationError("source_format 只能是 json 或 markdown")


@dataclass(frozen=True)
class TaskInputSnapshot(StrictModel):
    snapshot_id: str = ""; task_id: str = ""; task_card_hash: str = ""; resource_manifest_hash: str = ""; created_at: str = ""
@dataclass(frozen=True)
class ResourceManifest(StrictModel):
    manifest_id: str = ""; task_id: str = ""; resources: tuple = (); content_hash: str = ""; created_at: str = ""
@dataclass(frozen=True)
class ClarificationSet(StrictModel):
    clarification_id: str = ""; task_id: str = ""; questions: tuple = (); assumptions: tuple = (); confirmed: bool = False
@dataclass(frozen=True)
class NarrativeDocument(StrictModel):
    document_id: str = ""; task_id: str = ""; version: int = 1; markdown: str = ""; content_hash: str = ""; created_at: str = ""
@dataclass(frozen=True)
class SlideOutline(StrictModel):
    outline_id: str = ""; task_id: str = ""; version: int = 1; markdown: str = ""; slide_ids: tuple = (); content_hash: str = ""; created_at: str = ""
@dataclass(frozen=True)
class SampleSelection(StrictModel):
    selection_id: str = ""; task_id: str = ""; outline_hash: str = ""; slide_ids: tuple = (); confirmed: bool = False
@dataclass(frozen=True)
class DeckArtifact(StrictModel):
    artifact_id: str = ""; task_id: str = ""; version: int = 1; kind: str = "sample"; outline_hash: str = ""; content_hash: str = ""; created_at: str = ""
@dataclass(frozen=True)
class InspectionReport(StrictModel):
    report_id: str = ""; task_id: str = ""; deck_hash: str = ""; issues: tuple = (); passed: bool = False; created_at: str = ""
@dataclass(frozen=True)
class IssueDisposition(StrictModel):
    disposition_id: str = ""; task_id: str = ""; issue_id: str = ""; action: str = ""; actor: str = ""; created_at: str = ""
@dataclass(frozen=True)
class DeliveryManifest(StrictModel):
    delivery_id: str = ""; task_id: str = ""; deck_hash: str = ""; files: tuple = (); confirmed_by: str = ""; confirmed_at: str = ""


MODELS = (TaskCard, TaskInputSnapshot, ResourceManifest, ClarificationSet, NarrativeDocument,
          SlideOutline, SampleSelection, DeckArtifact, InspectionReport, IssueDisposition, DeliveryManifest)
