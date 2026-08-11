from __future__ import annotations

import html, re

from .errors import ValidationError

SLIDE = re.compile(r"^## \[([A-Za-z0-9_-]+)\]\s*(.*?)(?=^## \[|\Z)", re.M | re.S)
REMOTE = re.compile(r"(?:src|href)\s*=\s*['\"](?:https?:|//|file:|/)", re.I)
SCRIPT = re.compile(r"<(?:script|iframe|object|embed|form)\b", re.I)

def recommend(markdown: str, count: int = 2):
    slides=[(m.group(1),m.group(2)) for m in SLIDE.finditer(markdown)]
    if not slides: raise ValidationError("逐页大纲不包含有效页面")
    count=max(1,min(count,len(slides)))
    ranked=sorted(enumerate(slides),key=lambda x:(-(len(x[1])+40*("resources://" in x[1])),-x[0]))
    chosen=sorted(ranked[:count],key=lambda x:x[0])
    return [x[1][0] for x in chosen], {x[1][0]:"覆盖主要内容与资源版式" for x in chosen}

def render(markdown: str, slide_ids: list[str], rules=None, exceptions=None):
    blocks={m.group(1):m.group(2).strip() for m in SLIDE.finditer(markdown)}
    rules=rules or []; exceptions=exceptions or {}
    sections=[]
    for sid in slide_ids:
        text=blocks[sid]
        lines=[html.escape(x.strip(" -")) for x in text.splitlines() if x.strip() and not x.lstrip().startswith("!")]
        title=lines[0] if lines else sid
        body="".join(f"<p>{x}</p>" for x in lines[1:]) or "<p>内容已依据确认大纲生成</p>"
        note="".join(f"<small>{html.escape(x)}</small>" for x in exceptions.get(sid,[]))
        sections.append(f'<section class="slide" id="{sid}" data-slide-id="{sid}"><h1 data-element-id="title">{title}</h1><div data-element-id="body">{body}</div>{note}</section>')
    rule_text=" · ".join(html.escape(x) for x in rules)
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>html,body{margin:0;background:#111827;color:#f8fafc;font-family:system-ui}.slide{box-sizing:border-box;width:1280px;height:720px;padding:72px;margin:24px auto;background:linear-gradient(135deg,#172033,#253858);overflow:hidden}.slide h1{font-size:46px}.slide p{font-size:25px;line-height:1.5}small{display:block;color:#93c5fd}</style></head><body><aside hidden data-global-rules="'+rule_text+'"></aside>'+"".join(sections)+"</body></html>"

def validate_html(value: str, expected_ids: list[str]):
    if not isinstance(value,str) or not value.startswith("<!doctype html>"): raise ValidationError("HTML 构建结果无效")
    if SCRIPT.search(value) or REMOTE.search(value): raise ValidationError("HTML 包含不允许的主动内容或任务外资源")
    actual=re.findall(r'data-slide-id="([A-Za-z0-9_-]+)"',value)
    if actual != list(expected_ids): raise ValidationError("HTML 页面与样品选择不一致")
    if re.search(r"占位|placeholder|lorem ipsum",value,re.I): raise ValidationError("样品不得包含占位内容")
    return value
