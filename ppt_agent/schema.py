from __future__ import annotations

import re
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import datetime
from typing import Any, ClassVar, get_args, get_origin, get_type_hints

from .errors import ValidationError

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
HASH = re.compile(r"^[0-9a-f]{64}$")


def _schema(t):
    origin = get_origin(t)
    if origin is tuple: return {"type": "array", "items": {}}
    return {"type": {str: "string", int: "integer", bool: "boolean", dict: "object"}.get(t, "string")}


@dataclass(frozen=True)
class StrictModel:
    schema_version: str = "1.0"
    KIND: ClassVar[str] = "model"

    def __post_init__(self):
        hints = get_type_hints(type(self))
        for f in fields(self):
            value, expected = getattr(self, f.name), hints[f.name]
            origin = get_origin(expected)
            valid = isinstance(value, origin or expected) and not (expected is int and isinstance(value, bool))
            if not valid: raise ValidationError(f"{f.name} 类型无效")
        if self.schema_version != "1.0": raise ValidationError("不支持的 schema_version")
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name.endswith("_id") or f.name == "task_id":
                if not value or not ID.fullmatch(value): raise ValidationError(f"{f.name} 格式无效")
            if f.name.endswith("_hash") or f.name == "content_hash":
                if not HASH.fullmatch(value): raise ValidationError(f"{f.name} 必须是 sha256")
            if f.name.endswith("_at"):
                try: datetime.fromisoformat(value.replace("Z", "+00:00"))
                except (ValueError, AttributeError): raise ValidationError(f"{f.name} 必须是 ISO-8601 时间")
            if f.name == "version" and value < 1: raise ValidationError("version 必须大于零")

    @classmethod
    def parse(cls, value: dict[str, Any]):
        if not isinstance(value, dict): raise ValidationError(f"{cls.__name__} 必须是对象")
        allowed = {f.name for f in fields(cls)}
        if set(value) - allowed: raise ValidationError(f"{cls.__name__} 包含未知字段")
        required = {f.name for f in fields(cls) if f.name != "schema_version"}
        if required - set(value): raise ValidationError(f"{cls.__name__} 缺少必填字段")
        try: return cls(**value)
        except TypeError as exc: raise ValidationError(f"{cls.__name__} 格式无效") from exc

    def to_dict(self): return asdict(self)

    @classmethod
    def json_schema(cls):
        hints = get_type_hints(cls)
        props = {f.name: {"title": f.name, **_schema(hints[f.name])} for f in fields(cls)}
        for name in props:
            if name.endswith("_id") or name == "task_id": props[name]["pattern"] = ID.pattern
            if name.endswith("_hash") or name == "content_hash": props[name]["pattern"] = HASH.pattern
            if name.endswith("_at"): props[name]["format"] = "date-time"
        props["schema_version"]["const"] = "1.0"
        return {"$schema":"https://json-schema.org/draft/2020-12/schema","title":cls.__name__,"type":"object","additionalProperties":False,"properties":props,"required":[f.name for f in fields(cls) if f.name != "schema_version"]}

@dataclass(frozen=True)
class TaskCard(StrictModel):
    task_id:str=""; goal:str=""; audience:str=""; topic:str=""; source_format:str="json"
    def __post_init__(self):
        super().__post_init__()
        if not all((self.goal,self.audience,self.topic)): raise ValidationError("任务卡字段不得为空")
        if self.source_format not in {"json","markdown"}: raise ValidationError("source_format 无效")
@dataclass(frozen=True)
class TaskInputSnapshot(StrictModel): snapshot_id:str=""; task_id:str=""; task_card_hash:str=""; resource_manifest_hash:str=""; created_at:str=""
@dataclass(frozen=True)
class ResourceManifest(StrictModel): manifest_id:str=""; task_id:str=""; resources:tuple=(); content_hash:str=""; created_at:str=""
@dataclass(frozen=True)
class ClarificationSet(StrictModel): clarification_id:str=""; task_id:str=""; questions:tuple=(); assumptions:tuple=(); confirmed:bool=False
@dataclass(frozen=True)
class NarrativeDocument(StrictModel): document_id:str=""; task_id:str=""; version:int=1; markdown:str=""; content_hash:str=""; created_at:str=""
@dataclass(frozen=True)
class SlideOutline(StrictModel): outline_id:str=""; task_id:str=""; version:int=1; markdown:str=""; slide_ids:tuple=(); content_hash:str=""; created_at:str=""
@dataclass(frozen=True)
class SampleSelection(StrictModel): selection_id:str=""; task_id:str=""; outline_hash:str=""; slide_ids:tuple=(); confirmed:bool=False
@dataclass(frozen=True)
class DeckArtifact(StrictModel): artifact_id:str=""; task_id:str=""; version:int=1; kind:str="sample"; outline_hash:str=""; content_hash:str=""; created_at:str=""
@dataclass(frozen=True)
class InspectionReport(StrictModel): report_id:str=""; task_id:str=""; deck_hash:str=""; issues:tuple=(); passed:bool=False; created_at:str=""
@dataclass(frozen=True)
class IssueDisposition(StrictModel): disposition_id:str=""; task_id:str=""; issue_id:str=""; action:str=""; actor:str=""; created_at:str=""
@dataclass(frozen=True)
class DeliveryManifest(StrictModel): delivery_id:str=""; task_id:str=""; deck_hash:str=""; files:tuple=(); confirmed_by:str=""; confirmed_at:str=""

MODELS=(TaskCard,TaskInputSnapshot,ResourceManifest,ClarificationSet,NarrativeDocument,SlideOutline,SampleSelection,DeckArtifact,InspectionReport,IssueDisposition,DeliveryManifest)
