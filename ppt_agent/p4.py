from __future__ import annotations

import html, re
from html.parser import HTMLParser

from .errors import ValidationError

SLIDE = re.compile(r"^## \[([A-Za-z0-9_-]+)\]\s*(.*?)(?=^## \[|\Z)", re.M | re.S)
ALLOWED_TAGS = {"html", "head", "meta", "style", "body", "section", "aside", "div", "h1", "h2", "h3", "p", "small", "span", "strong", "em", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "br"}
GLOBAL_ATTRS = {"id", "class", "style", "lang", "hidden", "data-slide-id", "data-element-id", "data-global-rules"}
TAG_ATTRS = {"meta": {"charset", "name", "content"}, "th": {"colspan", "rowspan"}, "td": {"colspan", "rowspan"}}
CSS_PROPERTIES = {
    "align-items", "background", "background-color", "background-image", "border", "border-color", "border-radius",
    "border-style", "border-width", "box-sizing", "color", "display", "flex", "flex-direction", "font-family",
    "font-size", "font-style", "font-weight", "gap", "height", "justify-content", "letter-spacing", "line-height",
    "list-style", "margin", "margin-bottom", "margin-left", "margin-right", "margin-top", "max-height", "max-width",
    "min-height", "min-width", "opacity", "overflow", "padding", "padding-bottom", "padding-left", "padding-right",
    "padding-top", "text-align", "text-decoration", "text-transform", "width", "word-break"
}
SAFE_CSS_FUNCTIONS = {"calc", "clamp", "hsl", "hsla", "linear-gradient", "min", "max", "rgb", "rgba", "var"}

def _canonical(value: str) -> str:
    previous = value
    for _ in range(3):
        current = html.unescape(previous)
        if current == previous: break
        previous = current
    # CSS escapes contain up to six hex digits and may consume one whitespace.
    return re.sub(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)", lambda m: chr(int(m.group(1), 16)) if m.group(1) else m.group(2), previous)

def _validate_css(css: str) -> None:
    css = re.sub(r"/\*.*?\*/", "", _canonical(css), flags=re.S)
    if "@" in css or re.search(r"(?:https?|file|resources)\s*:|(?:^|[^:])/[/\\]|\.\.[/\\]", css, re.I):
        raise ValidationError("CSS 包含规则或任务外资源")
    functions = {name.lower() for name in re.findall(r"([_a-zA-Z-][_a-zA-Z0-9-]*)\s*\(", css)}
    if not functions <= SAFE_CSS_FUNCTIONS:
        raise ValidationError("CSS 包含不允许的函数")
    for block in re.findall(r"\{([^{}]*)\}", css, re.S) or [css]:
        for declaration in block.split(";"):
            if not declaration.strip(): continue
            if ":" not in declaration: raise ValidationError("CSS 声明无效")
            prop = declaration.split(":", 1)[0].strip().lower()
            if prop not in CSS_PROPERTIES: raise ValidationError("CSS 属性不在白名单")

class _SafeHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.errors=[]; self.styles=[]; self._in_style=False; self.slide_ids=[]
    def handle_starttag(self, tag, attrs): self._check(tag, attrs)
    def handle_startendtag(self, tag, attrs): self._check(tag, attrs)
    def handle_endtag(self, tag):
        if tag.lower() == "style": self._in_style=False
    def handle_data(self, data):
        if self._in_style: self.styles.append(data)
    def _check(self, tag, attrs):
        tag=tag.lower(); values={str(k).lower(): (v or "") for k,v in attrs}
        if tag not in ALLOWED_TAGS: self.errors.append(f"禁止标签 {tag}")
        allowed=GLOBAL_ATTRS | TAG_ATTRS.get(tag,set())
        if any(k not in allowed for k in values): self.errors.append("HTML 属性不在白名单")
        if "style" in values:
            try: _validate_css(values["style"])
            except ValidationError as exc: self.errors.append(str(exc))
        if tag == "style": self._in_style=True
        if "data-slide-id" in values: self.slide_ids.append(values["data-slide-id"])

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
    parser=_SafeHtmlParser()
    try: parser.feed(value); parser.close()
    except Exception as exc: raise ValidationError("HTML 语法无效") from exc
    try: _validate_css("\n".join(parser.styles))
    except ValidationError as exc: parser.errors.append(str(exc))
    if parser.errors:
        raise ValidationError("HTML 包含不允许的主动内容或任务外资源")
    if re.search(r"(?:\.\.[/\\]|file\s*:|https?\s*:|//|resources\s*://|tasks?[/\\])",_canonical(value),re.I):
        raise ValidationError("HTML 包含路径穿越、跨任务或未解析资源引用")
    actual=parser.slide_ids
    if actual != list(expected_ids): raise ValidationError("HTML 页面与样品选择不一致")
    if re.search(r"占位|placeholder|lorem ipsum",value,re.I): raise ValidationError("样品不得包含占位内容")
    return value
