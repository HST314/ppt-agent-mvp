from __future__ import annotations

import hashlib, json, mimetypes, re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import ConflictError, ValidationError

IMAGE_SUFFIXES={".png",".jpg",".jpeg",".webp",".gif",".svg"}
ALIASES={"goal":("goal","objective","演示目标","目标"),"audience":("audience","受众"),"topic":("topic","核心主题","主题")}
DEFAULTS={"language":"zh-CN","aspect_ratio":"16:9","sample_count":2}

def now(): return datetime.now(timezone.utc).isoformat()
def digest(data): return hashlib.sha256(data).hexdigest()
def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()

def parse_task_card(source, source_format):
    if source_format not in {"json","markdown"}: raise ValidationError("任务卡格式只能是 json 或 markdown")
    if source_format == "json":
        if isinstance(source,str):
            try: raw=json.loads(source)
            except json.JSONDecodeError as exc: raise ValidationError("JSON 任务卡格式无效") from exc
        else: raw=source
        if not isinstance(raw,dict): raise ValidationError("JSON 任务卡必须是对象")
    else:
        if not isinstance(source,str) or not source.strip(): raise ValidationError("Markdown 任务卡不得为空")
        raw={}
        for line in source.splitlines():
            m=re.match(r"^\s*(?:[-*]\s*)?(?:#{1,6}\s*)?([^:：]+?)\s*[:：]\s*(.+?)\s*$",line)
            if m: raw[m.group(1).strip()]=m.group(2).strip()
    normalized={}
    consumed=set()
    for target,names in ALIASES.items():
        for name in names:
            if name in raw and str(raw[name]).strip(): normalized[target]=str(raw[name]).strip(); consumed.add(name); break
    constraints={k:v for k,v in raw.items() if k not in consumed and k not in {"task_id","schema_version","source_format"}}
    missing=[key for key in ALIASES if key not in normalized]
    return {**normalized,"constraints":constraints,"defaults":DEFAULTS.copy(),"assumptions":[],"missing":missing,"source_format":source_format}

def scan_resources(root: Path):
    root=root.resolve(); entries=[]; warnings=[]; seen={}
    if not root.exists(): root.mkdir(parents=True)
    for path in sorted(root.rglob("*")):
        if path.is_symlink(): raise ValidationError("资源目录不允许符号链接")
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES: continue
        resolved=path.resolve()
        if root not in resolved.parents: raise ValidationError("资源路径越权")
        data=resolved.read_bytes()
        if not data: warnings.append({"code":"empty_resource","path":str(path.relative_to(root))}); continue
        h=digest(data); rel=path.relative_to(root).as_posix(); sidecar=path.with_suffix(".md")
        description=None
        if sidecar.exists():
            if sidecar.is_symlink() or root not in sidecar.resolve().parents: raise ValidationError("资源说明路径越权")
            description=sidecar.read_text(encoding="utf-8").strip() or None
        else: warnings.append({"code":"missing_sidecar","path":rel})
        if h in seen: warnings.append({"code":"duplicate_content","path":rel,"same_as":seen[h]})
        else: seen[h]=rel
        entries.append({"resource_id":f"resource-{h[:16]}","uri":f"resources://{rel}","media_type":mimetypes.guess_type(path.name)[0] or "application/octet-stream","content_hash":h,"description":description})
    return entries,warnings

def questions_for(card):
    labels={"goal":"本次演示最主要要达成什么目标？","audience":"演示的主要受众是谁？","topic":"演示的核心主题是什么？"}
    return [{"question_id":f"missing-{key}","field":key,"prompt":labels[key],"options":["稍后补充","使用 Other 自定义"],"allow_other":True,"blocking":True} for key in card["missing"]]

def validate_answer(question, answer):
    if not isinstance(answer,dict) or set(answer)-{"option","other"}: raise ValidationError("回答字段无效")
    option=answer.get("option")
    if option=="Other":
        if not isinstance(answer.get("other"),str) or not answer["other"].strip(): raise ValidationError("Other 必须提供自定义回答")
        return answer["other"].strip()
    if option not in question["options"]: raise ValidationError("回答不在可选项中")
    return option
