from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath


ASSET_ROOT = Path(__file__).with_name("offline_assets")


REMOTE_URL = re.compile(r"(?:https?:)?//[^\s\"'<>]+", re.I)
RUNTIME_TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".svg"}


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
        return REMOTE_URL.findall(without_comments)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.in_script = self.in_script or tag == "script"
        self.in_style = self.in_style or tag == "style"
        for name, value in attrs:
            name = name.lower()
            if value and name in self.URL_ATTRIBUTES:
                self.urls.extend(REMOTE_URL.findall(value))
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
.offline-player .slide[aria-hidden="false"]{display:block}
#offline-controls{position:fixed;z-index:2147483647;right:20px;bottom:20px;display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:12px;background:#111827e8;color:#fff;font:14px system-ui;box-shadow:0 4px 18px #0006}
#offline-controls button{min-width:44px;min-height:44px;border:1px solid #ffffff55;border-radius:8px;background:#ffffff18;color:#fff;font:inherit;cursor:pointer}
#offline-controls button:disabled{opacity:.35;cursor:not-allowed}#offline-page{min-width:64px;text-align:center}
</style>"""
    body = """<nav id="offline-controls" aria-label="幻灯片导航"><button id="offline-prev" type="button" aria-label="上一页">&#8592;</button><output id="offline-page" aria-live="polite"></output><button id="offline-next" type="button" aria-label="下一页">&#8594;</button></nav>
<script src="assets/offline-player.js"></script>
<script type="module" src="assets/motion.min.js"></script>"""
    lower = deck_html.lower()
    position = lower.rfind("</head>")
    deck_html = deck_html[:position] + head + deck_html[position:] if position >= 0 else deck_html.replace("<body", head + "<body", 1)
    position = deck_html.lower().rfind("</body>")
    return deck_html[:position] + body + deck_html[position:] if position >= 0 else deck_html + body


def offline_assets() -> dict[str, bytes]:
    names = ("offline-player.js", "motion.min.js", "THIRD_PARTY_NOTICES.txt")
    return {f"assets/{name}": (ASSET_ROOT / name).read_bytes() for name in names}
