from __future__ import annotations

import base64, binascii, hashlib, html, re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .design_contract import TemplateRegistry, validate_design_contract
from .errors import ValidationError
from .skill_runtime import SkillRuntime

SLIDE = re.compile(r"^## \[([A-Za-z0-9_-]+)\]\s*(.*?)(?=^## \[|\Z)", re.M | re.S)

# 允许的展示性 HTML 标签（包含语义化容器、排版、图文与 SVG）
ALLOWED_TAGS = {
    "html", "head", "meta", "title", "style", "body", "section", "aside", "div", "header", "footer", "main", "nav", "article",
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
    "box-decoration-break", "-webkit-box-decoration-break",
    "mask-image", "mask-repeat", "mask-size", "-webkit-mask-image", "-webkit-mask-repeat", "-webkit-mask-size",
    "-webkit-font-smoothing", "text-rendering", "will-change", "content", "animation",
    # 确定性溢出修复使用的惰性布局缩放（不加载资源、不执行脚本）
    "zoom",
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
    "linear-gradient", "radial-gradient", "conic-gradient", "url",
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
    "first-child", "last-child", "not", "is", "where", "has", "lang", "media"
}

IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_DATA_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_REFERENCES = 30
CSS_URL = re.compile(r"url\s*\(\s*(?:(['\"])(.*?)\1|([^)]*))\s*\)", re.I | re.S)

LOCKED_TEMPLATE_PATH = "assets/template.html"
# 生成页标题兜底规则不得覆盖模板自己的展示级字号类,否则整稿标题被压平
EDITORIAL_TITLE_GUARD = ":not(.display,.display-zh,.h1-zh,.h2-zh,.h3-zh,.h-hero,.h-xl,.h-sub,.h-md)"
SWISS_TITLE_GUARD = ":not(.h-hero,.h-hero-zh,.h-xl,.h-xl-zh,.h-md,.h-sub)"
# 模板展示类按 vw 设计(100vw 全屏页);生成页是固定 1280×720 画布,
# vw 只在 1280px 视口偶然成立。这里把各类钉到 1280 等效 px,保证任意视口与
# 离线缩放播放器中几何确定;主标题对 18px lead/正文保持 ≥8:1 的字号对比。
EDITORIAL_TEMPLATE_OVERRIDES = """
html,body{width:100%;height:auto;min-height:100%;overflow:auto;background:var(--ink)}
body{display:block;padding:24px 0}
.slide{box-sizing:border-box;width:1280px;height:720px;min-width:1280px;min-height:720px;flex:none;margin:0 auto 24px;overflow:hidden;background:var(--paper);color:var(--ink)}
.slide.light{background:var(--paper);color:var(--ink)}
.slide.dark{background:var(--ink);color:var(--paper)}
.slide>h1GUARD,.slide>h2GUARD,.slide [data-element-id="title"]GUARD{font-family:var(--serif-zh);font-size:52px;line-height:1.12;font-weight:700}
.slide .display{font-size:152px}
.slide .display-zh{font-size:148px}
.slide .h-hero{font-size:148px}
.slide .h1-zh{font-size:59px}
.slide .h2-zh{font-size:41px}
.slide .h3-zh{font-size:24px}
.slide .h-xl{font-size:79px}
.slide .h-sub{font-size:40px}
.slide .h-md{font-size:29px}
.slide .lead{font-size:18px}
.slide .big-num{font-size:128px}
.slide .mid-num{font-size:70px}
.slide .ghost{font-size:435px}
.slide .rowline .k{font-size:18px}
.slide .rowline .v{font-size:16px}
.slide .rowline .m{font-size:14px}
.slide .pipeline-label{font-size:14px}
.slide .step-nb{font-size:14px}
.slide .step-title{font-size:18px}
.slide .step-desc{font-size:16px}
.slide p,.slide li,.slide td,.slide th{font-family:var(--sans-zh);font-size:24px;line-height:1.5}
.slide small{font-size:16px;line-height:1.4}
.slide :focus-visible{outline:2px solid currentColor;outline-offset:2px}
@media (prefers-reduced-motion:reduce){.slide [data-anim]{opacity:1!important;transform:none!important}}
""".strip().replace("GUARD", EDITORIAL_TITLE_GUARD)
SWISS_TEMPLATE_OVERRIDES = """
html,body{width:100%;height:auto;min-height:100%;overflow:auto;background:var(--paper)}
body{display:block;padding:24px 0}
#deck{position:static;width:100%;height:auto;display:block;transform:none!important}
#nav,#hint,canvas.bg{display:none!important}
.slide{box-sizing:border-box;width:1280px;height:720px;min-width:1280px;min-height:720px;flex:none;margin:0 auto 24px;overflow:hidden}
.slide>h1GUARD,.slide>h2GUARD,.slide [data-element-id="title"]GUARD{font-family:var(--sans),var(--sans-zh);font-size:52px;line-height:1.08;font-weight:300}
.slide .h-hero{font-size:152px}
.slide .h-hero-zh{font-size:148px}
.slide .h-xl{font-size:77px}
.slide .h-xl-zh{font-size:64px}
.slide .h-md{font-size:33px}
.slide .h-sub{font-size:28px}
.slide .lead{font-size:18px}
.slide .num-mega,.slide .name-mega{font-size:115px}
.slide .kpi-hero{font-size:282px}
.slide .kpi-big{font-size:141px}
.slide .kpi-mid{font-size:77px}
.slide .kpi-thin{font-size:179px}
.slide .kpi-thin-sm{font-size:72px}
.slide .rowline .k{font-size:18px}
.slide .rowline .v{font-size:16px}
.slide .rowline .m{font-size:14px}
.slide .pipeline-label{font-size:14px}
.slide .step-nb{font-size:14px}
.slide .step-title{font-size:18px}
.slide .step-desc{font-size:16px}
.slide .step-meta{font-size:14px}
.slide p,.slide li,.slide td,.slide th{font-family:var(--sans),var(--sans-zh);font-size:24px;line-height:1.45}
.slide small{font-size:16px;line-height:1.4}
.slide :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.slide.dark :focus-visible,.slide.accent :focus-visible{outline-color:currentColor}
@media (prefers-reduced-motion:reduce){.slide [data-anim]{opacity:1!important;transform:none!important}}
""".strip().replace("GUARD", SWISS_TITLE_GUARD)
# Backwards-compatible constant retained for code importing the v1 name.
LOCKED_TEMPLATE_OVERRIDES = EDITORIAL_TEMPLATE_OVERRIDES

# 锁定模板的主题/品牌变量(两个风格的并集)。模型只允许消费 var(--*),
# 禁止在内联 style 里重定义,否则单主题(如 Swiss 的 IKB)会被局部篡改。
# 注意:只约束内联样式;锁定模板自身 :root 定义走 <style> 块路径,不受影响。
LOCKED_THEME_TOKENS = frozenset({
    "--accent", "--accent-rgb", "--accent-on", "--accent-bright",
    "--ink", "--ink-rgb", "--ink-tint", "--paper", "--paper-rgb", "--paper-tint",
    "--grey-1", "--grey-2", "--grey-3",
    "--text-primary", "--text-secondary", "--text-helper", "--text-placeholder", "--text-on-color",
    "--border-subtle", "--border-strong",
    "--sans", "--sans-zh", "--serif-en", "--serif-body-en", "--serif-zh", "--mono",
})


@lru_cache(maxsize=4)
def locked_template(style_id: str = "editorial") -> dict[str, str]:
    """Load the inert style layer from the hash-locked built-in template.

    The active scripts, external font links and example slides in the source
    template never enter a generated deck.  Only its single locked ``style``
    block is used, followed by fixed 1280x720 service-owned canvas overrides.
    """
    skill = SkillRuntime.builtin()
    registry = TemplateRegistry(skill)
    record = registry.resolve(style_id)
    source = skill.read_locked_text(record.asset_path)
    blocks = re.findall(r"<style(?:\s[^>]*)?>([\s\S]*?)</style\s*>", source, re.I)
    if len(blocks) != 1 or not blocks[0].strip():
        raise ValidationError("锁定 PPT 模板必须包含唯一非空 style 块")
    # The Swiss source uses one inline SVG mask for a decorative cross hatch.
    # Generated HTML deliberately keeps the stricter no-SVG-data-URL boundary;
    # omit that non-essential decoration from the inert server-owned style.
    style = re.sub(r"(?:-webkit-)?mask-image\s*:\s*url\([\s\S]*?\)\s*;", "", blocks[0], flags=re.I)
    return {
        "skill": skill.skill_name,
        "version": skill.skill_version,
        "template_id": record.template_id,
        "style_id": record.style_id,
        "path": record.asset_path,
        "sha256": record.template_hash,
        "style": style.strip() + "\n" + (SWISS_TEMPLATE_OVERRIDES if style_id == "swiss" else EDITORIAL_TEMPLATE_OVERRIDES),
    }


_H1_TITLE = re.compile(r"<h1\b[^>]*>([\s\S]*?)</h1\s*>", re.I)
_MARKED_TITLE = re.compile(
    r"<([a-z0-9]+)\b[^>]*\bdata-element-id\s*=\s*(['\"])title\2[^>]*>([\s\S]*?)</\1\s*>",
    re.I,
)


def _deck_title(sections) -> str:
    """Derive the document title from the first slide heading (fallback 演示文稿)."""
    for fragment in sections:
        match = _H1_TITLE.search(fragment) or _MARKED_TITLE.search(fragment)
        if not match:
            continue
        inner = match.group(1) if match.re is _H1_TITLE else match.group(3)
        text = " ".join(re.sub(r"<[^>]+>", "", html.unescape(inner)).split())
        if text:
            return text
    return "演示文稿"


def assemble_locked_template(sections, rules=None, design_contract=None, contract_hash=None) -> str:
    """Assemble validated slide fragments into the locked, script-free shell."""
    if design_contract is not None:
        validate_design_contract(design_contract)
        if not isinstance(contract_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", contract_hash):
            raise ValidationError("DesignContract hash 无效")
    template = locked_template(design_contract["style_id"] if design_contract else "editorial")
    rule_text = " · ".join(html.escape(str(item), quote=True) for item in (rules or []))
    provenance = html.escape(
        f"{template['skill']}@{template['version']}:{template['template_id']}:{template['path']}#{template['sha256']}",
        quote=True,
    )
    source = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        + f"<title>{html.escape(_deck_title(sections))}</title>"
        + (f'<meta name="design-contract" content="{contract_hash}">' if design_contract else "")
        + f'<meta name="ppt-template" content="{provenance}"><style>{template["style"]}</style>'
        + f'</head><body><aside hidden data-global-rules="{rule_text}" data-template="{provenance}"></aside>'
        + "".join(sections)
        + "</body></html>"
    )
    return apply_design_contract(source, design_contract, contract_hash) if design_contract else source


def _set_attribute(tag: str, name: str, value: str) -> str:
    encoded = html.escape(value, quote=True)
    pattern = re.compile(rf"\s{name}\s*=\s*(['\"])[\s\S]*?\1", re.I)
    replacement = f' {name}="{encoded}"'
    if pattern.search(tag):
        return pattern.sub(replacement, tag, count=1)
    return tag[:-1] + replacement + ">"


def _contract_fragment(fragment: str, item: dict[str, object], contract_hash: str) -> str:
    opening = re.match(r"<section\b[^>]*>", fragment, re.I)
    if not opening:
        raise ValidationError("DesignContract 页面不是 section 片段")
    tag = opening.group(0)
    classes_match = re.search(r"\bclass\s*=\s*(['\"])(.*?)\1", tag, re.I | re.S)
    classes = classes_match.group(2).split() if classes_match else []
    for name in ("slide", *str(item["theme"]).split("-")):
        if name and name not in classes:
            classes.append(name)
    for name, value in (
        ("class", " ".join(classes)),
        ("data-layout", str(item["layout_id"])),
        ("data-animate", str(item["animation_recipe"])),
        ("data-contract-hash", contract_hash),
    ):
        tag = _set_attribute(tag, name, value)
    fragment = tag + fragment[opening.end():]
    marker_count = len(re.findall(r"\bdata-anim(?:\s*=|\s|>)", fragment, re.I))
    needed = max(0, int(item["minimum_animation_markers"]) - marker_count)
    if needed:
        candidates = list(re.finditer(r"<(?:h1|h2|h3|p|div|ul|ol|table|svg)\b[^>]*>", fragment, re.I))
        offset = 0
        for index, match in enumerate(candidates[:needed]):
            original = match.group(0)
            if re.search(r"\bdata-anim(?:\s*=|\s|>)", original, re.I):
                continue
            replacement = original[:-1] + f' data-anim="contract-{index + 1}">'
            start, end = match.start() + offset, match.end() + offset
            fragment = fragment[:start] + replacement + fragment[end:]
            offset += len(replacement) - len(original)
        marker_count = len(re.findall(r"\bdata-anim(?:\s*=|\s|>)", fragment, re.I))
        if marker_count < int(item["minimum_animation_markers"]):
            raise ValidationError("DesignContract 页面缺少可登记的动效元素")
    return fragment


def apply_design_contract(html_text: str, design_contract: dict | None, contract_hash: str | None) -> str:
    """Bind server-owned template/layout facts to every generated slide."""
    if design_contract is None:
        return html_text
    validate_design_contract(design_contract)
    if not isinstance(contract_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", contract_hash):
        raise ValidationError("DesignContract hash 无效")
    fragments = {}
    # Local import avoids a dependency on TaskService's fragment helper.
    tag_re = re.compile(r"<section\b[^>]*>|</section\s*>", re.I)
    stack = []
    for match in tag_re.finditer(html_text):
        if match.group(0).lower().startswith("</"):
            if not stack:
                continue
            start, slide_id = stack.pop()
            if slide_id is not None:
                fragments[slide_id] = (start, match.end(), html_text[start:match.end()])
            continue
        tag = match.group(0)
        identifier = re.search(r"\b(?:data-slide-id|id)\s*=\s*(['\"])([A-Za-z0-9_-]+)\1", tag, re.I)
        classes = re.search(r"\bclass\s*=\s*(['\"])(.*?)\1", tag, re.I | re.S)
        is_slide = bool(classes and "slide" in classes.group(2).split())
        stack.append((match.start(), identifier.group(2) if is_slide and identifier and not stack else None))
    expected = {item["slide_id"]: item for item in design_contract["slide_contracts"]}
    if not fragments or not set(fragments).issubset(expected):
        raise ValidationError("DesignContract 与 HTML 页面范围不一致")
    for slide_id, (start, end, fragment) in sorted(fragments.items(), key=lambda pair: pair[1][0], reverse=True):
        replacement = _contract_fragment(fragment, expected[slide_id], contract_hash)
        html_text = html_text[:start] + replacement + html_text[end:]
    meta = f'<meta name="design-contract" content="{contract_hash}">'
    if re.search(r'<meta\b[^>]*name=["\']design-contract["\'][^>]*>', html_text, re.I):
        html_text = re.sub(r'<meta\b[^>]*name=["\']design-contract["\'][^>]*>', meta, html_text, count=1, flags=re.I)
    else:
        html_text = re.sub(r"<head\b[^>]*>", lambda match: match.group(0) + meta, html_text, count=1, flags=re.I)
    return html_text


def _decoded_url(value: str) -> str:
    decoded = _canonical(value).strip()
    for _ in range(3):
        current = unquote(decoded)
        if current == decoded:
            break
        decoded = current
    return decoded


def _valid_image_bytes(media_type: str, data: bytes) -> bool:
    return {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }.get(media_type, False)


def validate_image_url(value: str) -> str:
    """Validate one inert image reference without applying global text scans."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("图片 URL 不能为空")
    value = _decoded_url(value)
    lowered = value.lower()
    if lowered.startswith("data:"):
        match = re.fullmatch(r"data:(image/(?:png|jpeg|gif|webp));base64,([a-z0-9+/=\s]+)", value, re.I)
        if not match:
            raise ValidationError("Base64 图片格式或媒体类型不允许")
        try:
            data = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("Base64 图片内容无效") from exc
        media_type = match.group(1).lower()
        if len(data) > MAX_DATA_IMAGE_BYTES:
            raise ValidationError("Base64 图片超过 10 MiB 限制")
        if not _valid_image_bytes(media_type, data):
            raise ValidationError("Base64 图片内容与媒体类型不匹配")
        return value
    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValidationError("图片 URL 协议或凭据不允许")
        return value
    if value.startswith(("//", "/", "\\", "?", "#")) or "\\" in value:
        raise ValidationError("图片相对路径无效")
    path = PurePosixPath(parsed.path)
    if not parsed.path or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError("图片相对路径无效")
    return value


def _css_urls(css: str) -> list[str]:
    matches = list(CSS_URL.finditer(css))
    if len(re.findall(r"url\s*\(", css, re.I)) != len(matches):
        raise ValidationError("CSS url() 语法无效")
    urls = []
    for match in matches:
        value = (match.group(2) if match.group(1) else match.group(3) or "").strip()
        urls.append(validate_image_url(value))
    return urls


def _css_declarations(block: str) -> list[str]:
    declarations, current = [], []
    quote = None
    depth = 0
    for character in block:
        if quote:
            current.append(character)
            if character == quote:
                quote = None
        elif character in {"'", '"'}:
            quote = character
            current.append(character)
        elif character == "(":
            depth += 1
            current.append(character)
        elif character == ")":
            depth = max(0, depth - 1)
            current.append(character)
        elif character == ";" and depth == 0:
            declarations.append("".join(current))
            current = []
        else:
            current.append(character)
    declarations.append("".join(current))
    return declarations


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


def _validate_css(css: str, inline: bool = False) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", _canonical(css), flags=re.S)

    # 外部 URL 只允许出现在背景图片 url() 中；其余主动加载规则仍禁止。
    if re.search(r"@\s*(?:import|font-face|charset|namespace)\b", css, re.I):
        raise ValidationError("CSS 包含规则或任务外资源")

    # 检查 CSS 函数是否在安全白名单中
    functions = {name.lower() for name in re.findall(r"([_a-zA-Z-][_a-zA-Z0-9-]*)\s*\(", css)}
    unallowed = functions - SAFE_CSS_FUNCTIONS
    if unallowed:
        raise ValidationError(f"CSS 包含不允许的函数: {', '.join(sorted(unallowed))}")

    # 检查 CSS 属性
    urls = []
    for block in re.findall(r"\{([^{}]*)\}", css, re.S) or [css]:
        for declaration in _css_declarations(block):
            if not declaration.strip():
                continue
            if ":" not in declaration:
                raise ValidationError("CSS 声明无效")
            prop, value = (part.strip() for part in declaration.split(":", 1))
            prop = prop.lower()
            # 允许 CSS 变量 (--*) 以及白名单属性
            if not (prop.startswith("--") or prop in CSS_PROPERTIES):
                raise ValidationError(f"CSS 属性不在白名单: {prop}")
            # 内联样式不得重定义锁定主题变量(布局级变量如 --cols 仍允许)
            if inline and prop in LOCKED_THEME_TOKENS:
                raise ValidationError(f"内联样式禁止覆盖锁定主题变量: {prop}")
            declaration_urls = _css_urls(value)
            if declaration_urls and prop not in {"background", "background-image"}:
                raise ValidationError("CSS url() 仅允许用于背景图片")
            urls.extend(declaration_urls)
    return urls

class _SafeHtmlParser(HTMLParser):
    def __init__(self, allowed_assets=()):
        super().__init__(convert_charrefs=True)
        self.errors = []
        self.styles = []
        self._in_style = False
        self.slide_ids = []
        keys = allowed_assets.keys() if isinstance(allowed_assets, dict) else ()
        self.allowed_assets = {
            variant
            for uri in keys
            if str(uri).startswith("resources://")
            for variant in (str(uri).removeprefix("resources://"), f"resources/{str(uri).removeprefix('resources://')}")
        }
        self.image_references = 0

    def _record_images(self, references):
        for reference in references:
            parsed = urlsplit(reference)
            if not parsed.scheme and parsed.path.removeprefix("./") not in self.allowed_assets:
                self.errors.append("相对图片不属于当前冻结资源清单")
            self.image_references += 1

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
                self._record_images(_validate_css(values["style"], inline=True))
            except ValidationError as exc:
                self.errors.append(str(exc))
        if tag == "style":
            self._in_style = True
        if tag == "img":
            src = values.get("src", "")
            if src:
                try:
                    self._record_images([validate_image_url(src)])
                except ValidationError as exc:
                    self.errors.append(str(exc))
        if "data-slide-id" in values:
            self.slide_ids.append(values["data-slide-id"])


def recommend(markdown: str, count: int = 2, slide_contracts: list[dict] | None = None):
    """Choose a representative, diverse sample instead of the longest pages.

    A two-page automatic sample always contains the visual cover/hero and the
    strongest information-bearing non-cover page.  Additional slots are filled
    greedily for layout, role and resource diversity.  The score is deliberately
    deterministic so a frozen outline and DesignContract always produce the same
    selection and auditable reasons.
    """
    slides = [(m.group(1), m.group(2)) for m in SLIDE.finditer(markdown)]
    if not slides:
        raise ValidationError("逐页大纲不包含有效页面")
    count = max(1, min(count, len(slides)))
    contracts = {item.get("slide_id"): item for item in (slide_contracts or []) if isinstance(item, dict)}
    if slide_contracts is not None and (len(contracts) != len(slides) or set(contracts) != {sid for sid, _ in slides}):
        raise ValidationError("样品推荐所用 DesignContract 页面范围不完整或重复")

    candidates = []
    for index, (slide_id, body) in enumerate(slides):
        contract = contracts.get(slide_id, {})
        role = str(contract.get("visual_role") or ("cover" if index == 0 else "closing" if index == len(slides) - 1 else "body"))
        layout = str(contract.get("layout_id") or "unregistered")
        resource_count = len(re.findall(r"resources://[A-Za-z0-9_.\-/]+", body))
        structural_count = len(re.findall(r"(?m)^\s*(?:[-*+]\s|\d+[.)]\s|\|)", body))
        numeric_count = len(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?%?", body))
        decision_count = len(re.findall(r"风险|决策|取舍|预算|指标|里程碑|流程|架构|数据", body, re.I))
        visible_length = len(re.sub(r"\s+", "", body))
        density = visible_length + structural_count * 24 + numeric_count * 18 + decision_count * 32 + resource_count * 48
        candidates.append({
            "index": index,
            "slide_id": slide_id,
            "role": role,
            "layout": layout,
            "density": density,
            "resource_count": resource_count,
            "decision_count": decision_count,
        })

    cover = next((item for item in candidates if item["role"] in {"cover", "hero"}), candidates[0])
    selected = [cover]
    while len(selected) < count:
        selected_ids = {item["slide_id"] for item in selected}
        selected_layouts = {item["layout"] for item in selected}
        selected_roles = {item["role"] for item in selected}
        selected_resource_roles = {bool(item["resource_count"]) for item in selected}
        remaining = [item for item in candidates if item["slide_id"] not in selected_ids]
        if not remaining:
            break
        ranked = sorted(
            remaining,
            key=lambda item: (
                -(item["density"]
                  + (180 if item["layout"] not in selected_layouts else 0)
                  + (140 if item["role"] not in selected_roles else 0)
                  + (100 if bool(item["resource_count"]) not in selected_resource_roles else 0)
                  + (80 if item["decision_count"] else 0)
                  - (120 if item["role"] == "closing" and len(selected) == 1 else 0)),
                item["index"],
            ),
        )
        selected.append(ranked[0])

    chosen = sorted(selected, key=lambda item: item["index"])
    reasons = {}
    for item in chosen:
        if item["slide_id"] == cover["slide_id"]:
            reasons[item["slide_id"]] = f"代表封面/hero；visual_role={item['role']}；layout_id={item['layout']}"
        else:
            resource_role = "有资源" if item["resource_count"] else "无资源"
            reasons[item["slide_id"]] = (
                f"高信息与版式多样性；visual_role={item['role']}；layout_id={item['layout']}；"
                f"density={item['density']}；resource_role={resource_role}"
            )
    return [item["slide_id"] for item in chosen], reasons


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


def render(markdown: str, slide_ids: list[str], rules=None, exceptions=None, assets=None, design_contract=None, contract_hash=None):
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
    return assemble_locked_template(sections, rules, design_contract, contract_hash)


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
        parser._record_images(_validate_css("\n".join(parser.styles)))
    except ValidationError as exc:
        parser.errors.append(str(exc))
    if parser.errors:
        raise ValidationError(f"HTML 包含不允许的主动内容或任务外资源: {'; '.join(parser.errors[:3])}")
    if parser.image_references > MAX_IMAGE_REFERENCES:
        raise ValidationError(f"HTML 图片引用超过 {MAX_IMAGE_REFERENCES} 个限制")
    actual = parser.slide_ids
    if actual != list(expected_ids):
        raise ValidationError("HTML 页面与样品选择不一致")
    return value
