from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

from ..browser_inspection import ChromiumDeckInspector
from ..rendering.renderer import DeterministicRenderer
from ..rendering.validator import TechnicalValidator
from ..skill_runtime import ActiveSkillResolver
from .model_gateway import ModelGateway
from .pipeline import FileCheckpointStore, GenerationPipeline
from .stage_agent import StageAgentExecutor


class ResponsesProviderAdapter:
    """Expose create/retrieve through the existing narrow Responses client."""

    def __init__(self, client):
        self.client = client

    def create(self, **request: Any):
        return self.client.create(
            input=request["input"],
            tools=[],
            response_schema=request["response_schema"],
            timeout_seconds=request.get("timeout_seconds"),
        )

    def probe_capabilities(self) -> dict[str, bool]:
        """Prove the production model can enforce a strict JSON Schema."""
        turn = self.client.create(
            input=[{"role": "user", "content": "Return an object whose ready field is true."}],
            tools=[],
            response_schema={
                "name": "generation_core_readiness",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ready": {"type": "boolean", "const": True}},
                    "required": ["ready"],
                    "additionalProperties": False,
                },
            },
            timeout_seconds=min(float(self.client.config.request_timeout_seconds), 30.0),
        )
        payload = json.loads(turn.text)
        valid = payload == {"ready": True}
        return {"basic_response": valid, "json_schema": valid}

    def retrieve(self, response_id: str):
        request_client = self.client._request_client()
        try:
            response = request_client.responses.retrieve(response_id)
            return {
                "response_id": getattr(response, "id", response_id),
                "output": self.client._response_text(response),
                "status": getattr(response, "status", "completed"),
            }
        finally:
            if getattr(self.client, "_client", None) is None:
                request_client.close()


_CHROMIUM_EXECUTABLE_PATTERNS = (
    "chromium-*/chrome-win/chrome.exe",
    "chromium-*/chrome-linux/chrome",
    "chromium-*/chrome-linux64/chrome",
    "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "chromium-*/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
    "chromium_headless_shell-*/chrome-win/headless_shell.exe",
    "chromium_headless_shell-*/chrome-linux/headless_shell",
    "chromium_headless_shell-*/chrome-mac/headless_shell",
    "chromium_headless_shell-*/chrome-headless-shell-win64/headless_shell.exe",
    "chromium_headless_shell-*/chrome-headless-shell-linux64/headless_shell",
    "chromium_headless_shell-*/chrome-headless-shell-mac-*/headless_shell",
)


def _default_playwright_browser_root() -> Path:
    if sys.platform == "win32":
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "ms-playwright"
        return Path.home() / "AppData" / "Local" / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    cache_home = os.getenv("XDG_CACHE_HOME", "").strip()
    return (Path(cache_home) if cache_home else Path.home() / ".cache") / "ms-playwright"


def _find_managed_chromium(managed_roots: list[Path]) -> Path | None:
    for managed in managed_roots:
        for pattern in _CHROMIUM_EXECUTABLE_PATTERNS:
            candidates = sorted(managed.glob(pattern), reverse=True)
            if candidates:
                return candidates[0].resolve()
    return None


def resolve_chromium_executable(repository_root: str | Path | None = None) -> Path:
    configured = os.getenv("PPT_AGENT_CHROMIUM_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured).resolve()
        if not path.is_file():
            raise RuntimeError("PPT_AGENT_CHROMIUM_EXECUTABLE does not point to a file")
        return path
    root = Path(repository_root).resolve() if repository_root is not None else Path(__file__).resolve().parents[2]
    managed_roots = [root / ".playwright-browsers"]
    playwright_browsers_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if playwright_browsers_path and playwright_browsers_path != "0":
        managed_roots.append(Path(playwright_browsers_path).resolve())

    try:
        import playwright

        managed_roots.append(Path(playwright.__file__).resolve().parent / "driver" / "package" / ".local-browsers")
    except Exception:
        pass
    managed_roots.append(_default_playwright_browser_root())

    unique_roots = list(dict.fromkeys(path.resolve() for path in managed_roots))
    candidate = _find_managed_chromium(unique_roots)
    if candidate is not None and candidate.is_file():
        return candidate

    checked = ", ".join(str(path) for path in unique_roots)
    raise RuntimeError(
        "locked Chromium executable is unavailable; run "
        "`python -m playwright install chromium` with the active virtual environment "
        f"or set PPT_AGENT_CHROMIUM_EXECUTABLE; checked: {checked}"
    )


def build_generation_pipeline(config, *, data_root: str | Path, generation_client, repository_root: str | Path | None = None) -> GenerationPipeline | None:
    if config.mode == "fake":
        return None
    if config.generation.structured_output != "json_schema":
        raise RuntimeError("generation core requires structured_output=json_schema")
    provider = ResponsesProviderAdapter(generation_client)
    gateway = ModelGateway(
        provider,
        model=config.generation.model,
        timeout_seconds=config.generation.request_timeout_seconds,
        max_pre_dispatch_retries=1,
        secret_values=(config.generation.api_key,),
    )
    chromium = resolve_chromium_executable(repository_root)
    root = Path(data_root).resolve()
    resolver = ActiveSkillResolver(config.skills.root, config.skills.active)
    stage_agent = StageAgentExecutor(
        generation_client,
        resolver,
        model=config.generation.model,
        timeout_seconds=config.generation.run_timeout_seconds,
        max_steps=config.generation.max_steps,
        max_tool_calls=config.generation.max_tool_calls,
        max_provider_calls=config.generation.max_provider_calls,
        stage_budgets=config.generation.stage_budgets,
    )
    return GenerationPipeline(
        gateway,
        FileCheckpointStore(root / "generation-checkpoints"),
        DeterministicRenderer(),
        TechnicalValidator(ChromiumDeckInspector(executable_path=chromium), require_browser=True),
        asset_root=root,
        stage_agent=stage_agent,
        generation_mode=config.generation_mode,
    )
