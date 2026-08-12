from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path


REMOTE_URL = re.compile(r"(?:https?:)?//[^\s\"'<>]+", re.I)
TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".json", ".md", ".txt", ".svg"}


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
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        matches = sorted(set(REMOTE_URL.findall(path.read_text(encoding="utf-8"))))
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
