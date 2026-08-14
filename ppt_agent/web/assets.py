from __future__ import annotations

from pathlib import Path

from fastapi.responses import HTMLResponse


FRONTEND_BUILD = "2026.08.14.3"


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
