from __future__ import annotations

import re
from dataclasses import MISSING, asdict, dataclass, fields, is_dataclass
from datetime import datetime
from typing import Any, ClassVar, get_args, get_origin, get_type_hints

from .errors import ValidationError

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
HASH = re.compile(r"^[0-9a-f]{64}$")
NON_BLANK = r"^[\s\S]*\S[\s\S]*$"


def _schema(t):
    origin = get_origin(t)
    if origin is tuple:
        args=get_args(t); item=args[0] if args else Any
        return {"type": "array", "items": {} if item is Any else _schema(item)}
    if origin is dict: return {"type":"object"}
    if isinstance(t,type) and is_dataclass(t):
        hints=get_type_hints(t)
        return {"type":"object","additionalProperties":False,
                "properties":{f.name:_property_schema(f.name,hints[f.name]) for f in fields(t)},
                "required":[f.name for f in fields(t) if f.default is MISSING and f.default_factory is MISSING]}
    schema = {"type": {str: "string", int: "integer", bool: "boolean", dict: "object"}.get(t, "string")}
    if t is str:
        schema.update({"minLength": 1, "pattern": NON_BLANK})
    return schema

def _property_schema(name,t):
    prop={"title":name,**_schema(t)}
    if prop.get("type") == "string":
        prop.update({"minLength": 1, "pattern": NON_BLANK})
    if name.endswith("_id") or name == "task_id": prop["pattern"] = ID.pattern
    if name.endswith("_hash") or name == "content_hash": prop["pattern"] = HASH.pattern
    if name.endswith("_at"): prop["format"] = "date-time"
    if name == "version": prop["minimum"] = 1
    enums={"source_format":["json","markdown"],"kind":["sample","deck"],"action":["resolve","agent_fix","manual","waive","defer"],"actor":["user","system"],"confirmed_by":["user"],"severity":["warning","blocker"],"level":["element","slide","deck"]}
    if name in enums: prop["enum"]=enums[name]
    if name.endswith("_ids"): prop.update({"minItems":1,"uniqueItems":True,"items":{"type":"string","pattern":ID.pattern}})
    if name in {"slide_id","element_id"}: prop.update({"minLength":0,"pattern":rf"^(?:{ID.pattern[1:-1]})?$"})
    return prop

def _json_value(value, expected, name):
    """Validate JSON-shaped input and normalize arrays to immutable tuples."""
    origin=get_origin(expected)
    if origin is tuple:
        if not isinstance(value,(list,tuple)): raise ValidationError(f"{name} 类型无效")
        args=get_args(expected); item=args[0] if args else Any
        return tuple(value if item is Any else (_json_value(v,item,name) for v in value))
    if origin is dict:
        if not isinstance(value,dict): raise ValidationError(f"{name} 类型无效")
        return value
    if isinstance(expected,type) and is_dataclass(expected):
        if not isinstance(value,dict): raise ValidationError(f"{name} 类型无效")
        model_fields=fields(expected); allowed={f.name for f in model_fields}
        required={f.name for f in model_fields if f.default is MISSING and f.default_factory is MISSING}
        if set(value)-allowed or required-set(value): raise ValidationError(f"{name} 字段无效")
        hints=get_type_hints(expected)
        return expected(**{k:_json_value(v,hints[k],f"{name}.{k}") for k,v in value.items()})
    if expected is Any: return value
    if not isinstance(value,expected) or (expected is int and isinstance(value,bool)): raise ValidationError(f"{name} 类型无效")
    return value


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
            if isinstance(value,str) and not value.strip(): raise ValidationError(f"{f.name} 不得为空")
            if f.name.endswith("_id") or f.name == "task_id":
                if not value or not ID.fullmatch(value): raise ValidationError(f"{f.name} 格式无效")
            if f.name.endswith("_hash") or f.name == "content_hash":
                if not HASH.fullmatch(value): raise ValidationError(f"{f.name} 必须是 sha256")
            if f.name.endswith("_at"):
                try: parsed=datetime.fromisoformat(value.replace("Z", "+00:00"))
                except (ValueError, AttributeError): raise ValidationError(f"{f.name} 必须是 ISO-8601 时间")
                if parsed.tzinfo is None: raise ValidationError(f"{f.name} 必须包含时区")
            if f.name == "version" and value < 1: raise ValidationError("version 必须大于零")
            if f.name.endswith("_ids"):
                if not value or len(set(value)) != len(value) or any(not ID.fullmatch(x) for x in value): raise ValidationError(f"{f.name} 引用无效")
            if isinstance(value,tuple) and any(isinstance(x,str) and not x.strip() for x in value): raise ValidationError(f"{f.name} 元素不得为空")
            if isinstance(value,tuple):
                for item in value:
                    validate=getattr(item,"validate",None)
                    if validate: validate()

    @classmethod
    def parse(cls, value: dict[str, Any]):
        if not isinstance(value, dict): raise ValidationError(f"{cls.__name__} 必须是对象")
        allowed = {f.name for f in fields(cls)}
        if set(value) - allowed: raise ValidationError(f"{cls.__name__} 包含未知字段")
        optional_compat={"level","element_id","evidence","suggestion","source","target_deck_hash","rationale"}
        required = {f.name for f in fields(cls) if f.name != "schema_version" and f.name not in optional_compat}
        if required - set(value): raise ValidationError(f"{cls.__name__} 缺少必填字段")
        hints=get_type_hints(cls)
        normalized={name:_json_value(item,hints[name],name) for name,item in value.items()}
        try: return cls(**normalized)
        except TypeError as exc: raise ValidationError(f"{cls.__name__} 格式无效") from exc

    def to_dict(self): return asdict(self)

    @classmethod
    def json_schema(cls):
        hints = get_type_hints(cls)
        props = {f.name:_property_schema(f.name,hints[f.name]) for f in fields(cls)}
        props["schema_version"]["const"] = "1.0"
        optional_compat={"level","element_id","evidence","suggestion","source","target_deck_hash","rationale"}
        return {"$schema":"https://json-schema.org/draft/2020-12/schema","title":cls.__name__,"type":"object","additionalProperties":False,"properties":props,"required":[f.name for f in fields(cls) if f.name != "schema_version" and f.name not in optional_compat]}

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
class ResourceEntry:
    resource_id:str; uri:str; media_type:str; content_hash:str
    def validate(self):
        if not ID.fullmatch(self.resource_id) or not self.uri.strip() or not self.media_type.strip() or not HASH.fullmatch(self.content_hash): raise ValidationError("resource 字段语义无效")
@dataclass(frozen=True)
class ResourceManifest(StrictModel): manifest_id:str=""; task_id:str=""; resources:tuple[ResourceEntry,...]=(); content_hash:str=""; created_at:str=""
@dataclass(frozen=True)
class ClarificationSet(StrictModel): clarification_id:str=""; task_id:str=""; questions:tuple[str,...]=(); assumptions:tuple[str,...]=(); confirmed:bool=False
@dataclass(frozen=True)
class NarrativeDocument(StrictModel):
    document_id:str=""; task_id:str=""; version:int=1; markdown:str=""; content_hash:str=""; created_at:str=""
    def __post_init__(self):
        super().__post_init__()
        if not self.markdown.strip(): raise ValidationError("markdown 不得为空")
@dataclass(frozen=True)
class SlideOutline(StrictModel): outline_id:str=""; task_id:str=""; version:int=1; markdown:str=""; slide_ids:tuple[str,...]=(); content_hash:str=""; created_at:str=""
@dataclass(frozen=True)
class SampleSelection(StrictModel): selection_id:str=""; task_id:str=""; outline_hash:str=""; slide_ids:tuple[str,...]=(); confirmed:bool=False
@dataclass(frozen=True)
class DeckArtifact(StrictModel):
    artifact_id:str=""; task_id:str=""; version:int=1; kind:str="sample"; outline_hash:str=""; content_hash:str=""; created_at:str=""
    def __post_init__(self):
        super().__post_init__()
        if self.kind not in {"sample","deck"}: raise ValidationError("kind 无效")
@dataclass(frozen=True)
class InspectionIssue:
    issue_id:str; severity:str; code:str; message:str; slide_id:str
    level:str="slide"; element_id:str=""; evidence:str="未提供独立证据"; suggestion:str="请人工检查并修复"
    source:str="semantic_model"
    def validate(self):
        if (not ID.fullmatch(self.issue_id) or self.severity not in {"warning","blocker"} or self.level not in {"element","slide","deck"}
            or self.source not in {"semantic_model","semantic_deterministic","technical_browser"}
            or not self.code.strip() or not self.message.strip() or not self.evidence.strip() or not self.suggestion.strip()): raise ValidationError("issue 字段语义无效")
        if self.slide_id and not ID.fullmatch(self.slide_id): raise ValidationError("issue 页面范围无效")
        if self.element_id and not ID.fullmatch(self.element_id): raise ValidationError("issue 元素范围无效")
@dataclass(frozen=True)
class InspectionReport(StrictModel): issues:tuple[InspectionIssue,...]=(); report_id:str=""; task_id:str=""; deck_hash:str=""; passed:bool=False; created_at:str=""
@dataclass(frozen=True)
class IssueDisposition(StrictModel):
    disposition_id:str=""; task_id:str=""; issue_id:str=""; action:str=""; actor:str=""; created_at:str=""; target_deck_hash:str="0"*64; rationale:str="未记录依据"
    def __post_init__(self):
        super().__post_init__()
        if self.action not in {"resolve","agent_fix","manual","waive","defer"} or self.actor not in {"user","system"}: raise ValidationError("问题处置语义无效")
@dataclass(frozen=True)
class DeliveryManifest(StrictModel):
    delivery_id:str=""; task_id:str=""; deck_hash:str=""; files:tuple[str,...]=(); confirmed_by:str=""; confirmed_at:str=""
    def __post_init__(self):
        super().__post_init__()
        if self.confirmed_by != "user" or not self.files: raise ValidationError("交付确认语义无效")

MODELS=(TaskCard,TaskInputSnapshot,ResourceManifest,ClarificationSet,NarrativeDocument,SlideOutline,SampleSelection,DeckArtifact,InspectionReport,IssueDisposition,DeliveryManifest)
