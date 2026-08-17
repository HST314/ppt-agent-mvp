from __future__ import annotations

import base64, hashlib, html, re
from html.parser import HTMLParser

from .errors import ValidationError

SLIDE = re.compile(r"^## \[([A-Za-z0-9_-]+)\]\s*(.*?)(?=^## \[|\Z)", re.M | re.S)

# 允许的展示性 HTML 标签（包含语义化容器、排版、图文与 SVG）
ALLOWED_TAGS = {
    "html", "head", "meta", "style", "body", "section", "aside", "div", "header", "footer", "main", "nav", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "small", "span", "strong", "b", "em", "i", "u", "s", "sub", "sup",
    "blockquote", "cite", "q", "mark", "code", "pre", "hr", "br",
    "figure", "figcaption",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tr", "th", "td",
    "img", "canvas",
    "svg", "g", "path", "circle", "line", "rect", "polyline", "polygon", "ellipse", "text", "defs", "use", "marker"
}

# 全局通用属性白名单（允许 data-*、aria-* 以及常用无障碍/展示属性）
GLOBAL_ATTRS = {
    "id", "class", "style", "lang", "hidden", "title", "role", "dir", "tabindex"
}

# 特定标签属性白名单
TAG_ATTRS = {
    "meta": {"charset", "name", "content"},
    "th": {"colspan", "rowspan", "scope"},
    "td": {"colspan", "rowspan"},
    "img": {"src", "alt", "loading", "width", "height"},
    "svg": {"viewbox", "xmlns", "width", "height", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "preserveaspectratio", "transform", "opacity"},
    "g": {"fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "transform", "opacity"},
    "path": {"d", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "transform", "opacity", "marker-end", "marker-start"},
    "circle": {"cx", "cy", "r", "fill", "stroke", "stroke-width", "transform", "opacity"},
    "line": {"x1", "y1", "x2", "y2", "stroke", "stroke-width", "stroke-linecap", "stroke-dasharray", "transform", "opacity"},
    "rect": {"x", "y", "width", "height", "rx", "ry", "fill", "stroke", "stroke-width", "transform", "opacity"},
    "polyline": {"points", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "transform", "opacity"},
    "polygon": {"points", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "transform", "opacity"},
    "ellipse": {"cx", "cy", "rx", "ry", "fill", "stroke", "stroke-width", "transform", "opacity"},
    "text": {"x", "y", "dx", "dy", "text-anchor", "dominant-baseline", "font-family", "font-size", "font-weight", "fill", "stroke", "transform", "opacity"},
}

# CSS 属性白名单（扩充现代排版、Grid、Flex、定位、Border 等属性）
CSS_PROPERTIES = {
    # 布局与盒模型
    "display", "box-sizing", "width", "height", "min-width", "min-height", "max-width", "max-height",
    "margin", "margin-top", "margin-bottom", "margin-left", "margin-right", "margin-inline", "margin-block",
    "padding", "padding-top", "padding-bottom", "padding-left", "padding-right", "padding-inline", "padding-block",
    "overflow", "overflow-x", "overflow-y", "aspect-ratio", "visibility", "opacity",
    # 定位
    "position", "top", "bottom", "left", "right", "inset", "z-index",
    # Flexbox
    "flex", "flex-grow", "flex-shrink", "flex-basis", "flex-direction", "flex-wrap", "flex-flow",
    "justify-content", "align-items", "align-content", "align-self", "justify-items", "justify-self", "gap", "row-gap", "column-gap",
    # CSS Grid
    "grid", "grid-template-columns", "grid-template-rows", "grid-column", "grid-row", "grid-auto-rows", "grid-auto-flow", "grid-area",
    # 排版与字体
    "font", "font-family", "font-size", "font-weight", "font-style", "font-feature-settings",
    "line-height", "letter-spacing", "text-align", "text-decoration", "text-transform", "text-indent",
    "white-space", "word-break", "overflow-wrap", "word-wrap", "text-overflow", "vertical-align",
    "list-style", "list-style-type",
    # 背景与边框
    "color", "background", "background-color", "background-image", "background-size", "background-position", "background-repeat",
    "border", "border-width", "border-style", "border-color", "border-radius",
    "border-top", "border-top-width", "border-top-style", "border-top-color",
    "border-bottom", "border-bottom-width", "border-bottom-style", "border-bottom-color",
    "border-left", "border-left-width", "border-left-style", "border-left-color",
    "border-right", "border-right-width", "border-right-style", "border-right-color",
    "border-collapse", "border-spacing", "outline", "outline-width", "outline-style", "outline-color", "outline-offset",
    "box-shadow", "text-shadow",
    # 视觉效果
    "transform", "transform-origin", "transition", "transition-property", "transition-duration", "transition-timing-function",
    "object-fit", "object-position", "cursor", "pointer-events", "user-select",
    "mix-blend-mode", "filter", "backdrop-filter", "-webkit-backdrop-filter",
    # SVG 样式
    "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-opacity", "fill-opacity"
}

# 允许的安全 CSS 函数与选择器伪类
SAFE_CSS_FUNCTIONS = {
    # 尺寸与数学计算
    "calc", "clamp", "min", "max", "var", "env",
    "abs", "sign", "round", "mod", "rem",
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "pow", "sqrt", "hypot", "log", "exp",
    # 颜色
    "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color-mix", "color", "light-dark",
    # 渐变
    "linear-gradient", "radial-gradient", "conic-gradient",
    "repeating-linear-gradient", "repeating-radial-gradient",
    # 2D / 3D 变形
    "translate", "translatex", "translatey", "translatez", "translate3d",
    "scale", "scalex", "scaley", "scalez", "scale3d",
    "rotate", "rotatex", "rotatey", "rotatez", "rotate3d",
    "skew", "skewx", "skewy", "matrix", "matrix3d", "perspective",
    # 滤镜
    "blur", "brightness", "contrast", "drop-shadow", "grayscale", "hue-rotate", "invert", "opacity", "saturate", "sepia",
    # 剪裁与图形
    "polygon", "circle", "ellipse", "inset", "rect", "path",
    # CSS Grid 网格与布局
    "repeat", "minmax", "fit-content",
    # 动画与缓动曲线
    "cubic-bezier", "steps",
    # 选择器伪类/伪元素（防止样式表选择器中的括号被误判为未授权函数）
    "nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type",
    "first-child", "last-child", "not", "is", "where", "has", "lang"
}


def _canonical(value: str) -> str:
    previous = value
    for _ in range(3):
        current = html.unescape(previous)
        if current == previous:
            break
        previous = current
    result = []
    index = 0
    while index < len(previous):
        if previous[index] != "\\":
            result.append(previous[index])
            index += 1
            continue
        if index + 1 == len(previous) or previous[index + 1] in "\n\r\f":
            raise ValidationError("CSS 包含截断或无效 escape")
        index += 1
        start = index
        while index < len(previous) and index - start < 6 and previous[index] in "0123456789abcdefABCDEF":
            index += 1
        if index > start:
            codepoint = int(previous[start:index], 16)
            if codepoint == 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                raise ValidationError("CSS escape 包含无效码点")
            result.append(chr(codepoint))
            if index < len(previous) and previous[index] in " \t\n\r\f":
                if previous[index] == "\r" and index + 1 < len(previous) and previous[index + 1] == "\n":
                    index += 1
                index += 1
        else:
            result.append(previous[index])
            index += 1
    return "".join(result)


def _validate_css(css: str) -> None:
    css = re.sub(r"/\*.*?\*/", "", _canonical(css), flags=re.S)
    
    # 拦截 @import、外部资源引用与路径穿越，但允许本地 @media / @keyframes 规则
    if re.search(r"@\s*(?:import|font-face|charset|namespace)\b", css, re.I) or re.search(
        r"(?:https?|file|resources)\s*:|(?:^|[^:])/[/\\]|\.\.[/\\]", css, re.I
    ):
        raise ValidationError("CSS 包含规则或任务外资源")
        
    # 检查 CSS 函数是否在安全白名单中
    functions = {name.lower() for name in re.findall(r"([_a-zA-Z-][_a-zA-Z0-9-]*)\s*\(", css)}
    unallowed = functions - SAFE_CSS_FUNCTIONS
    if unallowed:
        raise ValidationError(f"CSS 包含不允许的函数: {', '.join(sorted(unallowed))}")
        
    # 检查 CSS 属性
    for block in re.findall(r"\{([^{}]*)\}", css, re.S) or [css]:
        for declaration in block.split(";"):
            if not declaration.strip():
                continue
            if ":" not in declaration:
                raise ValidationError("CSS 声明无效")
            prop = declaration.split(":", 1)[0].strip().lower()
            # 允许 CSS 变量 (--*) 以及白名单属性
            if not (prop.startswith("--") or prop in CSS_PROPERTIES):
                raise ValidationError(f"CSS 属性不在白名单: {prop}")

class _SafeHtmlParser(HTMLParser):
    def __init__(self, allowed_assets=()):
        super().__init__(convert_charrefs=True)
        self.errors = []
        self.styles = []
        self._in_style = False
        self.slide_ids = []
        self.allowed_assets = set(allowed_assets)

    def handle_starttag(self, tag, attrs):
        self._check(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._check(tag, attrs)

    def handle_endtag(self, tag):
        if tag.lower() == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.styles.append(data)

    def _check(self, tag, attrs):
        tag = tag.lower()
        values = {str(k).lower(): (v or "") for k, v in attrs}
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"禁止标签 {tag}")
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, set())
        for k, v in values.items():
            # 允许任何 data-* 与 aria-* 属性，拒绝所有 on* 事件属性
            if k.startswith("on"):
                self.errors.append(f"禁止事件属性 {k}")
            elif not (k.startswith("data-") or k.startswith("aria-") or k in allowed):
                self.errors.append(f"HTML 属性不在白名单: {k}")

        if "style" in values:
            try:
                _validate_css(values["style"])
            except ValidationError as exc:
                self.errors.append(str(exc))
        if tag == "style":
            self._in_style = True
        if tag == "img":
            src = values.get("src", "")
            if src and src not in self.allowed_assets:
                self.errors.append("图片不属于当前冻结资源清单")
        if "data-slide-id" in values:
            self.slide_ids.append(values["data-slide-id"])


def recommend(markdown: str, count: int = 2):
    slides = [(m.group(1), m.group(2)) for m in SLIDE.finditer(markdown)]
    if not slides:
        raise ValidationError("逐页大纲不包含有效页面")
    count = max(1, min(count, len(slides)))
    ranked = sorted(enumerate(slides), key=lambda x: (-(len(x[1]) + 40 * ("resources://" in x[1])), -x[0]))
    chosen = sorted(ranked[:count], key=lambda x: x[0])
    return [x[1][0] for x in chosen], {x[1][0]: "覆盖主要内容与资源版式" for x in chosen}


def controlled_assets(manifest: dict, resource_root):
    """Resolve only frozen, hash-matching manifest resources to inert data URLs."""
    assets = {}
    for item in manifest.get("resources", []):
        uri = item.get("uri", "")
        if not re.fullmatch(r"resources://[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", uri):
            raise ValidationError("资源引用格式无效")
        path = (resource_root / uri.removeprefix("resources://")).resolve()
        if resource_root.resolve() not in path.parents or not path.is_file():
            raise ValidationError("冻结资源不存在或路径越权")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item.get("content_hash"):
            raise ValidationError("冻结资源内容已变化")
        media = item.get("media_type", "")
        if media not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise ValidationError("资源媒体类型不允许进入预览")
        assets[uri] = f"data:{media};base64,{base64.b64encode(data).decode()}"
    return assets


def infer_scope(prompt: str, slide_id=None, element_id=None, requested=None):
    text = prompt.strip().lower()
    global_hint = bool(re.search(r"整稿|全局|所有页|每一页|统一|global|all slides", text))
    page_hint = bool(re.search(r"当前页|这一页|本页|指定页|page|slide-[a-z0-9_-]+", text))
    element_hint = bool(re.search(r"标题|正文|图表|图片|元素|title|body|element", text))
    if global_hint and (page_hint or element_hint):
        raise ValidationError("修改范围存在歧义，请明确是全局、页面还是元素")
    inferred = "global" if global_hint else ("element" if element_hint and element_id else ("page" if (page_hint or slide_id) else "global"))
    if requested is not None and requested not in {"global", "page", "element"}:
        raise ValidationError("修改作用域无效")
    if requested and requested != inferred and (global_hint or page_hint or element_hint):
        raise ValidationError("显式作用域与 Prompt 语义冲突，请确认修改范围")
    scope = requested or inferred
    if scope == "element" and not element_id:
        raise ValidationError("Prompt 指向元素，但未选择具体元素")
    if scope in {"page", "element"} and not slide_id:
        raise ValidationError("Prompt 指向局部，但未选择具体页面")
    return scope, {
        "scope": scope,
        "basis": "prompt_semantics" if (global_hint or page_hint or element_hint) else "current_selection",
        "slide_id": slide_id if scope != "global" else None,
        "element_id": element_id if scope == "element" else None
    }


def render(markdown: str, slide_ids: list[str], rules=None, exceptions=None, assets=None):
    blocks = {m.group(1): m.group(2).strip() for m in SLIDE.finditer(markdown)}
    rules = rules or []
    exceptions = exceptions or {}
    sections = []
    for sid in slide_ids:
        text = blocks[sid]
        lines = [html.escape(x.strip(" -")) for x in text.splitlines() if x.strip() and not re.search(r"!\[[^\]]*\]\(resources://[^)]+\)", x)]
        title = lines[0] if lines else sid
        body = "".join(f"<p>{x}</p>" for x in lines[1:]) or "<p>内容已依据确认大纲生成</p>"
        note = "".join(f"<small>{html.escape(x)}</small>" for x in exceptions.get(sid, []))
        images = []
        for alt, uri in re.findall(r"!\[([^\]]*)\]\((resources://[^)]+)\)", text):
            if uri not in (assets or {}):
                raise ValidationError("大纲引用不属于当前冻结资源清单")
            images.append(f'<img data-element-id="resource" src="{html.escape(assets[uri], quote=True)}" alt="{html.escape(alt, quote=True)}">')
        sections.append(f'<section class="slide" id="{sid}" data-slide-id="{sid}"><h1 data-element-id="title">{title}</h1><div data-element-id="body">{body}</div>{"".join(images)}{note}</section>')
    rule_text = " · ".join(html.escape(x) for x in rules)
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>html,body{margin:0;background:#111827;color:#f8fafc;font-family:system-ui}.slide{box-sizing:border-box;width:1280px;height:720px;padding:72px;margin:24px auto;background:linear-gradient(135deg,#172033,#253858);overflow:hidden}.slide h1{font-size:46px}.slide p{font-size:25px;line-height:1.5}small{display:block;color:#93c5fd}</style></head><body><aside hidden data-global-rules="' + rule_text + '"></aside>' + "".join(sections) + "</body></html>"


def validate_html(value: str, expected_ids: list[str], allowed_assets=()):
    if not isinstance(value, str) or not value.startswith("<!doctype html>"):
        raise ValidationError("HTML 构建结果无效")
    parser = _SafeHtmlParser(allowed_assets)
    try:
        parser.feed(value)
        parser.close()
    except Exception as exc:
        raise ValidationError("HTML 语法无效") from exc
    try:
        _validate_css("\n".join(parser.styles))
    except ValidationError as exc:
        parser.errors.append(str(exc))
    if parser.errors:
        raise ValidationError(f"HTML 包含不允许的主动内容或任务外资源: {'; '.join(parser.errors[:3])}")
    
    # 检查非受控的远程或敏感协议（排除 SVG xmlns 等安全 URL）
    scrubbed = value
    for asset in allowed_assets:
        scrubbed = scrubbed.replace(asset, "")
    scrubbed = re.sub(r'xmlns="http://www.w3.org/2000/svg"', '', scrubbed)
    scrubbed = re.sub(r'xmlns="http://www.w3.org/1999/xlink"', '', scrubbed)

    if re.search(r"(?:\.\.[/\\]|file\s*:|resources\s*://|tasks?[/\\]|data\s*:)", _canonical(scrubbed), re.I):
        raise ValidationError("HTML 包含路径穿越、跨任务或未解析资源引用")
    actual = parser.slide_ids
    if actual != list(expected_ids):
        raise ValidationError("HTML 页面与样品选择不一致")
    if re.search(r"占位|placeholder|lorem ipsum", value, re.I):
        raise ValidationError("样品不得包含占位内容")
    return value