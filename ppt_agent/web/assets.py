from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi.responses import HTMLResponse


FRONTEND_BUILD = "2026.08.17.112846263255"


def _commit_sha(value: str) -> str:
    value = value.strip()
    return value[:40] if re.fullmatch(r"[0-9a-fA-F]{7,64}", value) else "unknown"


def backend_commit() -> str:
    configured = os.environ.get("PPT_AGENT_COMMIT_SHA", "").strip()
    if configured:
        return _commit_sha(configured)
    dot_git = Path(__file__).resolve().parents[2] / ".git"
    try:
        if dot_git.is_file():
            pointer = dot_git.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir: "):
                return "unknown"
            dot_git = (dot_git.parent / pointer.removeprefix("gitdir: ")).resolve()
        head = (dot_git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return _commit_sha(head)
        reference = head.removeprefix("ref: ")
        loose = dot_git / reference
        if loose.is_file():
            return _commit_sha(loose.read_text(encoding="utf-8"))
        packed = dot_git / "packed-refs"
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith(("#", "^")):
                commit, name = line.split(" ", 1)
                if name == reference:
                    return _commit_sha(commit)
    except (OSError, ValueError):
        pass
    return "unknown"


def index_response(frontend_root: Path) -> HTMLResponse:
    source = (frontend_root / "index.html").read_text(encoding="utf-8")
    html = source.replace("__BUILD_ID__", FRONTEND_BUILD)
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "X-PPT-Agent-Build": FRONTEND_BUILD,
        },
    )
