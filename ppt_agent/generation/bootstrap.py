from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..browser_inspection import ChromiumDeckInspector
from ..rendering.renderer import DeterministicRenderer
from ..rendering.validator import TechnicalValidator
from .model_gateway import ModelGateway
from .pipeline import FileCheckpointStore, GenerationPipeline


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
    for managed in managed_roots:
        candidates = sorted(managed.glob("chromium_headless_shell-*/chrome-linux/headless_shell"), reverse=True)
        if not candidates:
            candidates = sorted(managed.glob("chromium-*/chrome-linux/chrome"), reverse=True)
        if candidates:
            return candidates[0].resolve()

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            candidate = Path(playwright.chromium.executable_path).resolve()
        if candidate.is_file():
            return candidate
    except Exception:
        pass
    raise RuntimeError("locked Chromium executable is unavailable")


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
    return GenerationPipeline(
        gateway,
        FileCheckpointStore(root / "generation-checkpoints"),
        DeterministicRenderer(),
        TechnicalValidator(ChromiumDeckInspector(executable_path=chromium), require_browser=True),
        asset_root=root,
    )
