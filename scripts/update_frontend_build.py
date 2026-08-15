#!/usr/bin/env python3
"""Issue and verify a unique frontend cache key for every asset change."""

from __future__ import annotations

import argparse
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
ASSETS_MODULE = ROOT / "ppt_agent" / "web" / "assets.py"
MANIFEST = FRONTEND / ".build-source.sha256"
BUILD_RE = re.compile(r'^FRONTEND_BUILD = "([^"]+)"$', re.MULTILINE)
QUERY_RE = re.compile(r"[?&]v=([^\"']+)")


def source_files() -> list[Path]:
    return sorted(
        path
        for path in FRONTEND.rglob("*")
        if path.is_file() and path != MANIFEST and path.suffix in {".html", ".css", ".js"}
    )


def current_build() -> str:
    match = BUILD_RE.search(ASSETS_MODULE.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("FRONTEND_BUILD is missing")
    return match.group(1)


def source_digest(build: str) -> str:
    digest = hashlib.sha256()
    for path in source_files():
        relative = path.relative_to(ROOT).as_posix()
        normalized = path.read_text(encoding="utf-8").replace(build, "__FRONTEND_BUILD__")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(normalized.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def verify() -> None:
    build = current_build()
    if not MANIFEST.is_file() or MANIFEST.read_text(encoding="utf-8").strip() != source_digest(build):
        raise SystemExit("frontend assets changed without issuing a new build key")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    tokens = set(QUERY_RE.findall(index))
    for path in (FRONTEND / "static" / "js").rglob("*.js"):
        tokens.update(QUERY_RE.findall(path.read_text(encoding="utf-8")))
    if tokens != {build}:
        raise SystemExit(f"frontend build keys are inconsistent: {sorted(tokens)}")


def issue(build: str | None) -> None:
    previous = current_build()
    build = build or datetime.now(timezone.utc).strftime("%Y.%m.%d.%H%M%S%f")
    if not re.fullmatch(r"[0-9A-Za-z._-]{8,64}", build) or build == previous:
        raise SystemExit("new build key must be unique and contain only URL-safe characters")
    paths = [ASSETS_MODULE, *source_files()]
    replacements = 0
    for path in paths:
        source = path.read_text(encoding="utf-8")
        updated = source.replace(previous, build)
        if updated != source:
            path.write_text(updated, encoding="utf-8")
            replacements += 1
    if replacements < 2:
        raise SystemExit("frontend build key was not referenced by the expected assets")
    MANIFEST.write_text(source_digest(build) + "\n", encoding="utf-8")
    verify()
    print(build)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--build")
    args = parser.parse_args()
    if args.check:
        verify()
    else:
        issue(args.build)


if __name__ == "__main__":
    main()
