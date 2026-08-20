from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import stat
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urljoin, urlsplit

from .errors import ValidationError
from .p4 import CSS_URL, IMAGE_MEDIA_TYPES, _valid_image_bytes, validate_image_url


ASSET_ROOT = Path(__file__).with_name("offline_assets")


REMOTE_URL = re.compile(r"(?:https?:)?//[^\s\"'<>]+", re.I)
CSS_IMPORT = re.compile(r"@import\s+(?:url\(\s*)?(['\"])(.*?)\1\s*\)?", re.I | re.S)
RUNTIME_TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".svg"}
MAX_REMOTE_IMAGES = 30
MAX_REMOTE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REMOTE_TOTAL_BYTES = 50 * 1024 * 1024
REMOTE_TIMEOUT_SECONDS = 10
IMAGE_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def _without_js_comments(source: str) -> str:
    """Remove JS comments without damaging URL strings in executable code."""
    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if quote:
            output.append(char)
            if char == "\\" and index + 1 < len(source):
                index += 1
                output.append(source[index])
            elif char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
            output.append(char)
        elif char == "/" and following == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            output.append("\n")
        elif char == "/" and following == "*":
            index += 2
            while index + 1 < len(source) and source[index:index + 2] != "*/":
                index += 1
            index += 1
        else:
            output.append(char)
        index += 1
    return "".join(output)


class _HTMLRuntimeURLs(HTMLParser):
    URL_ATTRIBUTES = {"src", "href", "action", "poster", "data", "formaction", "srcset"}

    def __init__(self, depth: int = 0):
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.in_script = False
        self.in_style = False
        self.depth = depth

    @staticmethod
    def _css_urls(source: str) -> list[str]:
        without_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        candidates = _css_image_urls(without_comments)
        candidates.extend(match.group(2).strip() for match in CSS_IMPORT.finditer(without_comments))
        return [value for value in candidates if _external_reference(value)]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.in_script = self.in_script or tag == "script"
        self.in_style = self.in_style or tag == "style"
        for name, value in attrs:
            name = name.lower()
            if value and name in self.URL_ATTRIBUTES:
                if name == "srcset":
                    self.urls.extend(REMOTE_URL.findall(value))
                elif _external_reference(value):
                    self.urls.append(value.strip())
            elif value and name == "style":
                self.urls.extend(self._css_urls(value))
            elif value and name == "srcdoc":
                if self.depth >= 32:
                    raise ValueError("iframe srcdoc nesting exceeds offline validation limit")
                nested = _HTMLRuntimeURLs(self.depth + 1)
                nested.feed(value)
                nested.close()
                self.urls.extend(nested.urls)

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag.lower() == "script":
            self.in_script = False
        elif tag.lower() == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_script:
            self.urls.extend(REMOTE_URL.findall(_without_js_comments(data)))
        elif self.in_style:
            self.urls.extend(self._css_urls(data))


class _ImageReferences(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        values = {str(name).lower(): value or "" for name, value in attrs}
        if tag.lower() == "img" and values.get("src"):
            self.urls.append(values["src"])
        if values.get("style"):
            self.urls.extend(_css_image_urls(values["style"]))
        if tag.lower() == "style":
            self.in_style = True

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag.lower() == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            self.urls.extend(_css_image_urls(data))


def _css_image_urls(source: str) -> list[str]:
    return [((match.group(2) if match.group(1) else match.group(3)) or "").strip() for match in CSS_URL.finditer(source)]


def _external_reference(value: str) -> bool:
    """Classify one parsed runtime reference without scanning inside data payloads."""
    candidate = value.strip()
    parsed = urlsplit(candidate)
    return parsed.scheme.lower() in {"http", "https"} or candidate.startswith("//")


def _public_http_target(value: str):
    value = validate_image_url(value)
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValidationError("远程图片仅支持 HTTP/HTTPS")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValidationError("远程图片域名解析失败") from exc
    if not addresses:
        raise ValidationError("远程图片域名没有可用地址")
    public = []
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValidationError("远程图片地址指向本机、内网或保留网段")
        public.append(str(ip))
    return value, parsed, sorted(set(public), key=lambda item:(ipaddress.ip_address(item).version,int(ipaddress.ip_address(item))))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the validated IP while retaining hostname TLS validation."""
    def __init__(self,host,address,port,timeout):
        super().__init__(host,port,timeout=timeout,context=ssl.create_default_context())
        self.address=address

    def connect(self):
        raw=socket.create_connection((self.address,self.port),self.timeout,self.source_address)
        try:
            self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def download_remote_image(url: str) -> tuple[bytes, str]:
    for redirects in range(6):
        connection=None
        try:
            url,parsed,addresses=_public_http_target(url)
            host=parsed.hostname.encode("idna").decode("ascii")
            port=parsed.port or (443 if parsed.scheme.lower()=="https" else 80)
            connection=(_PinnedHTTPSConnection(host,addresses[0],port,REMOTE_TIMEOUT_SECONDS) if parsed.scheme.lower()=="https" else http.client.HTTPConnection(addresses[0],port,timeout=REMOTE_TIMEOUT_SECONDS))
            target=parsed.path or "/"
            if parsed.query: target+=f"?{parsed.query}"
            default_port=443 if parsed.scheme.lower()=="https" else 80
            host_header=host if port==default_port else f"{host}:{port}"
            connection.request("GET",target,headers={"Host":host_header,"Accept":"image/png,image/jpeg,image/gif,image/webp","Accept-Encoding":"identity","User-Agent":"PPT-Agent-Offline-Packager/1.0"})
            response=connection.getresponse()
            if response.status in {301,302,303,307,308}:
                location=response.headers.get("Location")
                connection.close()
                if not location: raise ValidationError("远程图片重定向缺少目标")
                if redirects==5: raise ValidationError("远程图片重定向次数过多")
                url=urljoin(url,location)
                continue
            if response.status<200 or response.status>=300:
                connection.close()
                raise ValidationError("远程图片下载失败")
            media_type = response.headers.get_content_type().lower()
            if media_type not in IMAGE_MEDIA_TYPES:
                connection.close()
                raise ValidationError("远程资源不是受支持的图片类型")
            length = response.headers.get("Content-Length")
            if length and int(length)>MAX_REMOTE_IMAGE_BYTES:
                connection.close()
                raise ValidationError("远程图片超过 10 MiB 限制")
            data = response.read(MAX_REMOTE_IMAGE_BYTES + 1)
            connection.close()
        except ValidationError:
            if connection is not None: connection.close()
            raise
        except (OSError,ValueError,http.client.HTTPException,ssl.SSLError) as exc:
            if connection is not None: connection.close()
            raise ValidationError("远程图片下载失败") from exc
        if len(data)>MAX_REMOTE_IMAGE_BYTES:
            raise ValidationError("远程图片超过 10 MiB 限制")
        if not _valid_image_bytes(media_type,data):
            raise ValidationError("远程图片内容与媒体类型不匹配")
        return data,media_type
    raise ValidationError("远程图片重定向次数过多")


def _manifest_resource(url: str, manifest: dict, resource_root: Path) -> tuple[str, bytes, str]:
    parsed = urlsplit(url)
    path = parsed.path.removeprefix("./")
    if path.startswith("resources/"):
        path = path.removeprefix("resources/")
    matches = {
        item.get("uri", "").removeprefix("resources://"): item
        for item in manifest.get("resources", [])
    }
    item = matches.get(path)
    if not item:
        raise ValidationError(f"相对图片不属于当前冻结资源清单: {url}")
    source = (resource_root / path).resolve()
    if resource_root.resolve() not in source.parents or not source.is_file():
        raise ValidationError("相对图片不存在或路径越权")
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != item.get("content_hash"):
        raise ValidationError("相对图片内容已变化")
    if item.get("media_type") not in IMAGE_MEDIA_TYPES:
        raise ValidationError("相对资源不是受支持的图片类型")
    return f"resources/{PurePosixPath(path).as_posix()}", data, item["media_type"]


def localize_delivery_html(deck_html: str, manifest: dict, resource_root: Path, fetcher=download_remote_image) -> tuple[str, dict[str, bytes], list[dict]]:
    """Localize every remote/relative image while preserving inert Base64 images."""
    parser = _ImageReferences()
    parser.feed(deck_html)
    parser.close()
    unique = list(dict.fromkeys(parser.urls))
    remote = [url for url in unique if urlsplit(url).scheme.lower() in {"http", "https"}]
    if len(remote) > MAX_REMOTE_IMAGES:
        raise ValidationError(f"远程图片超过 {MAX_REMOTE_IMAGES} 个限制")
    replacements: dict[str, str] = {}
    files: dict[str, bytes] = {}
    records = []
    total = 0
    for url in unique:
        validate_image_url(url)
        parsed = urlsplit(url)
        if parsed.scheme.lower() == "data":
            continue
        if parsed.scheme.lower() in {"http", "https"}:
            data, media_type = fetcher(url)
            total += len(data)
            if total > MAX_REMOTE_TOTAL_BYTES:
                raise ValidationError("远程图片总大小超过 50 MiB 限制")
            name = f"resources/remote-{hashlib.sha256((url + hashlib.sha256(data).hexdigest()).encode()).hexdigest()[:20]}{IMAGE_EXTENSIONS[media_type]}"
            source = "remote"
        else:
            name, data, media_type = _manifest_resource(url, manifest, resource_root)
            source = "relative"
        if name in files and files[name] != data:
            raise ValidationError("离线资源文件名冲突")
        files[name] = data
        replacements[url] = name
        records.append({"source": source, "original_url": url, "path": name, "media_type": media_type, "sha256": hashlib.sha256(data).hexdigest()})

    def replace_css(match):
        quote = match.group(1) or '"'
        value = ((match.group(2) if match.group(1) else match.group(3)) or "").strip()
        return f"url({quote}{replacements.get(value, value)}{quote})"

    def replace_img(match):
        prefix, quote, quoted, bare = match.groups()
        value = quoted if quote else bare
        replacement = replacements.get(value, value)
        return f"{prefix}{quote or ''}{replacement}{quote or ''}"

    localized = CSS_URL.sub(replace_css, deck_html)
    localized = re.sub(r"(<img\b[^>]*?\bsrc\s*=\s*)(?:(['\"])(.*?)\2|([^\s>]+))", replace_img, localized, flags=re.I | re.S)
    return localized, files, records


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def delivery_files(root: Path) -> list[Path]:
    root = root.resolve()
    return sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: path.relative_to(root).as_posix())


def external_urls(root: Path) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {}
    for path in delivery_files(root):
        suffix = path.suffix.lower()
        if suffix not in RUNTIME_TEXT_SUFFIXES:
            continue
        source = path.read_text(encoding="utf-8")
        if suffix in {".html", ".htm"}:
            parser = _HTMLRuntimeURLs()
            parser.feed(source)
            matches = sorted(set(parser.urls))
        elif suffix in {".js", ".mjs"}:
            matches = sorted(set(REMOTE_URL.findall(_without_js_comments(source))))
        else:
            matches = sorted(set(REMOTE_URL.findall(re.sub(r"/\*.*?\*/", "", source, flags=re.S))))
        if matches:
            findings[path.relative_to(root).as_posix()] = matches
    return findings


def verify_delivery(root: Path) -> dict[str, str]:
    root = root.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("delivery manifest.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("delivery manifest files is empty or invalid")
    actual = {path.relative_to(root).as_posix(): sha256(path) for path in delivery_files(root) if path != manifest_path}
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(name for name in set(actual) & set(expected) if actual[name] != expected[name])
        raise ValueError(f"delivery hash mismatch: missing={missing}, extra={extra}, changed={changed}")
    urls = external_urls(root)
    if urls:
        raise ValueError(f"delivery contains external URLs: {urls}")
    html = root / "deck.html"
    if not html.is_file() or "<html" not in html.read_text(encoding="utf-8").lower():
        raise ValueError("deck.html is missing or invalid")
    return actual


def build_zip(root: Path, output: Path) -> str:
    """Create a byte-stable ZIP after verifying the immutable delivery directory."""
    root, output = root.resolve(), output.resolve()
    if output == root or root in output.parents:
        raise ValueError("output ZIP must be outside the delivery directory")
    verify_delivery(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in delivery_files(root):
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256(output)


def validate_zip_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Reject members that are ambiguous or unsafe on POSIX or Windows."""
    members = archive.infolist()
    seen: set[str] = set()
    for member in members:
        name = member.filename
        windows = PureWindowsPath(name)
        posix = PurePosixPath(name)
        normalized = name.replace("\\", "/")
        parts = normalized.split("/")
        if (
            not name
            or name.startswith(("/", "\\"))
            or posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(f"ZIP contains an unsafe path: {name!r}")
        if normalized in seen:
            raise ValueError(f"ZIP contains a duplicate member: {name!r}")
        seen.add(normalized)

        unix_mode = member.external_attr >> 16
        file_type = stat.S_IFMT(unix_mode)
        if member.is_dir() or (file_type and file_type != stat.S_IFREG):
            raise ValueError(f"ZIP contains a non-regular member: {name!r}")
    return members


def offline_player(deck_html: str) -> str:
    """Turn the frozen deck into a local-first slide player."""
    head = """<style id="offline-player-style">
body.offline-player{overflow:hidden}.offline-player .slide{display:none;margin:0 auto}
.offline-player .slide[aria-hidden="false"]{display:block;position:fixed;left:50%;top:var(--offline-center-y,50%);margin:0;transform:translate(-50%,-50%) scale(var(--offline-scale,1));transform-origin:center}
#offline-controls{position:fixed;z-index:2147483647;left:50%;right:auto;bottom:max(12px,env(safe-area-inset-bottom));transform:translateX(-50%);display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:12px;background:#111827e8;color:#fff;font:14px system-ui;box-shadow:0 4px 18px #0006}
#offline-controls button{min-width:44px;min-height:44px;border:1px solid #ffffff55;border-radius:8px;background:#ffffff18;color:#fff;font:inherit;cursor:pointer}
#offline-controls button:disabled{opacity:.35;cursor:not-allowed}#offline-page{min-width:64px;text-align:center}
</style>"""
    body = """<nav id="offline-controls" aria-label="幻灯片导航"><button id="offline-prev" type="button" aria-label="上一页">&#8592;</button><output id="offline-page" aria-live="polite"></output><button id="offline-next" type="button" aria-label="下一页">&#8594;</button></nav>
<script src="assets/motion.min.js"></script>
<script src="assets/offline-player.js"></script>"""
    lower = deck_html.lower()
    position = lower.rfind("</head>")
    deck_html = deck_html[:position] + head + deck_html[position:] if position >= 0 else deck_html.replace("<body", head + "<body", 1)
    position = deck_html.lower().rfind("</body>")
    return deck_html[:position] + body + deck_html[position:] if position >= 0 else deck_html + body


def offline_assets() -> dict[str, bytes]:
    names = ("offline-player.js", "motion.min.js", "THIRD_PARTY_NOTICES.txt")
    return {f"assets/{name}": (ASSET_ROOT / name).read_bytes() for name in names}
