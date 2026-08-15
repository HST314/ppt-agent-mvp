from __future__ import annotations

import hashlib, json, mimetypes, re, struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import ConflictError, ValidationError

IMAGE_SUFFIXES={".png",".jpg",".jpeg",".webp",".gif",".svg"}
ALIASES={"goal":("goal","objective","演示目标","目标"),"audience":("audience","受众"),"topic":("topic","核心主题","主题")}
DEFAULTS={"language":"zh-CN","aspect_ratio":"16:9","sample_count":2}
PRESENTATION_WORDS=r"(?:ppt|演示文稿|幻灯片|演示)"

def now(): return datetime.now(timezone.utc).isoformat()
def digest(data): return hashlib.sha256(data).hexdigest()
def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()

def valid_image_content(data, suffix):
    """Validate the container enough to reject mislabeled or truncated images."""
    suffix=suffix.lower()
    if suffix==".png":
        return len(data)>=33 and data[:8]==b"\x89PNG\r\n\x1a\n" and data[12:16]==b"IHDR" and struct.unpack(">II",data[16:24])!=(0,0)
    if suffix in {".jpg",".jpeg"}:
        return len(data)>=4 and data[:3] in {b"\xff\xd8\xff"} and data[-2:]==b"\xff\xd9"
    if suffix==".gif":
        return len(data)>=14 and data[:6] in {b"GIF87a",b"GIF89a"} and data[-1:]==b";" and struct.unpack("<HH",data[6:10])!=(0,0)
    if suffix==".webp":
        return len(data)>=16 and data[:4]==b"RIFF" and data[8:12]==b"WEBP" and struct.unpack("<I",data[4:8])[0]+8==len(data)
    if suffix==".svg":
        if b"<!DOCTYPE" in data.upper(): return False
        try:
            import xml.etree.ElementTree as ET
            root=ET.fromstring(data)
        except (ET.ParseError,UnicodeDecodeError,ValueError):
            return False
        return root.tag.rsplit("}",1)[-1].lower()=="svg"
    return False

def detect_source_format(source):
    if isinstance(source,dict): return "json"
    if not isinstance(source,str) or not source.strip(): raise ValidationError("任务卡不得为空")
    try:
        value=json.loads(source)
        if isinstance(value,dict): return "json"
    except json.JSONDecodeError: pass
    return "markdown"

def infer_natural_language_task_card(source):
    """Conservatively retain facts explicitly present in a short brief."""
    if not isinstance(source,str): return {},[]
    text=re.sub(r"\s+"," ",source).strip()
    if not text: return {},[]
    facts={}; assumptions=[]
    match=re.search(rf"(?:关于|围绕|用于)\s*(.+?)\s*(?:的)?\s*{PRESENTATION_WORDS}\b",text,re.IGNORECASE)
    if not match:
        match=re.search(rf"(?:制作|设计|生成|做)\s*(?:一个|一份|一套)?\s*(.+?)\s*(?:的)?\s*{PRESENTATION_WORDS}\b",text,re.IGNORECASE)
    if match:
        topic=match.group(1).strip(" ，。,:：的")
        topic=re.sub(r"^(?:一个|一份|一套)\s*","",topic)
    else:
        # A short, single-line, non-key/value brief is commonly a title.  Keep
        # that explicit text as the topic, but never infer an audience.
        title=text.strip(" ，。")
        title_like=(
            1 <= len(title) <= 80
            and "\n" not in source
            and not re.search(r"[:：\n]",text)
            and not re.match(r"^(?:[-*#]|\{|\[)",text)
            and not re.search(r"[！？；!?;]",text)
        )
        if not title_like: return facts,assumptions
        topic=title
    if not topic: return facts,assumptions
    facts["topic"]=topic
    goal_prefixes={"介绍":"介绍","汇报":"汇报","发布":"发布","培训":"培训讲解","复盘":"复盘"}
    matched_action=next((suffix for suffix in goal_prefixes if topic.endswith(suffix) and topic[:-len(suffix)].strip()),None)
    if matched_action:
        facts["goal"]=f"{goal_prefixes[matched_action]}{topic[:-len(matched_action)].strip()}"
    elif match:
        facts["goal"]=f"制作关于{topic}的演示"
    assumptions.append("topic 根据原始自然语言中的明确表述提取；仅在标题包含明确动作时提取 goal，未推断受众。")
    return facts,assumptions

def parse_task_card(source, source_format="auto"):
    if source_format not in {"auto","json","markdown"}: raise ValidationError("任务卡格式只能是 auto、json 或 markdown")
    requested=source_format
    detected=detect_source_format(source)
    if source_format != "auto" and source_format != detected:
        raise ValidationError(f"任务卡内容识别为 {detected}，与声明的 {source_format} 不一致")
    source_format=detected
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
    inferred,assumptions=infer_natural_language_task_card(source if isinstance(source,str) else "")
    applied_inference=False
    for key,value in inferred.items():
        if not any(name in raw for name in ALIASES[key]): raw[key]=value; applied_inference=True
    if not applied_inference: assumptions=[]
    normalized={}
    consumed=set()
    for target,names in ALIASES.items():
        for name in names:
            if name in raw and str(raw[name]).strip(): normalized[target]=str(raw[name]).strip(); consumed.add(name); break
        if target not in normalized:
            for container in (raw.get("known_facts"), raw.get("task_card"), raw.get("brief")):
                if isinstance(container,dict):
                    for name in names:
                        if name in container and str(container[name]).strip(): normalized[target]=str(container[name]).strip(); break
                if target in normalized: break
    constraints={k:v for k,v in raw.items() if k not in consumed and k not in {"task_id","schema_version","source_format"}}
    missing=[key for key in ALIASES if key not in normalized]
    return {**normalized,"constraints":constraints,"defaults":DEFAULTS.copy(),"assumptions":assumptions,"missing":missing,"source_format":source_format,"format_detection":{"requested":requested,"detected":detected,"confidence":"high"}}

def scan_resources(root: Path):
    root=root.resolve(); entries=[]; warnings=[]; seen={}
    if not root.exists(): root.mkdir(parents=True)
    for path in sorted(root.rglob("*")):
        if path.is_symlink(): raise ValidationError("资源目录不允许符号链接")
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES: continue
        resolved=path.resolve()
        if root not in resolved.parents: raise ValidationError("资源路径越权")
        rel=path.relative_to(root).as_posix()
        if resolved.stat().st_size > 16*1024*1024:
            warnings.append({"path":rel,"code":"resource_too_large"}); continue
        data=resolved.read_bytes()
        if not data: warnings.append({"code":"empty_resource","path":str(path.relative_to(root))}); continue
        if not valid_image_content(data,path.suffix):
            warnings.append({"code":"invalid_image_content","path":rel}); continue
        h=digest(data); sidecar=path.with_suffix(".md")
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
    specs={
      "goal":("这份演示完成后，最希望受众采取什么行动？",["理解并形成共识","批准方案或预算","做出购买决策","用于培训或知识传递"]),
      "audience":("这份演示主要面向哪类受众？",["公司管理层","客户或潜在客户","项目团队","公众或活动观众"]),
      "topic":("请确认本次演示聚焦的核心主题。",["产品或服务介绍","项目方案与汇报","行业研究与洞察","培训与知识分享"]),
    }
    result=[]
    for key in card["missing"]:
        prompt, values=specs[key]
        result.append({"question_id":f"missing-{key}","field_path":key,"field":key,"prompt":prompt,
          "helper_text":"请选择最符合实际情况的一项，也可以填写自定义答案。",
          "options":[{"value":value,"label":value,"description":""} for value in values],
          "allow_other":True,"blocking":True})
    return result[:6]

def validate_answer(question, answer):
    if not isinstance(answer,dict) or set(answer)-{"option","other"}: raise ValidationError("回答字段无效")
    option=answer.get("option")
    if option=="Other":
        if not isinstance(answer.get("other"),str) or not answer["other"].strip(): raise ValidationError("Other 必须提供自定义回答")
        return answer["other"].strip()
    # Keep accepting the v1.0.1 deferred-answer token for API compatibility;
    # new clients no longer present it as a suggested answer.
    values=[item["value"] if isinstance(item,dict) else item for item in question["options"]]
    if option != "稍后补充" and option not in values: raise ValidationError("回答不在可选项中")
    return option
