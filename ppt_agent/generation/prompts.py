from __future__ import annotations

from typing import Any

from .contracts import CONTRACT_VERSION, NarrativeSpec, OutlineDraft, SampleSpec, SlideBatchSpec, canonical_json


PROMPT_VERSION = "1.0"


def _request(stage: str, contract, payload: dict[str, Any], rules: tuple[str, ...]) -> list[dict[str, str]]:
    system = "\n".join((
        f"You produce the {stage} content payload for a presentation service.",
        "Return exactly one JSON object matching the supplied JSON Schema.",
        "Do not return HTML, CSS, scripts, workflow state, checkpoint IDs other than values supplied by the service, or prose outside JSON.",
        "Use only evidence identifiers and immutable identifiers supplied in the input.",
        "Keep every slide concise: no more than four visible content blocks and no more than 420 visible characters including the title.",
        *rules,
    ))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": canonical_json({"prompt_version": PROMPT_VERSION, "contract_version": CONTRACT_VERSION, "contract": contract.TITLE, "input": payload})},
    ]


def narrative_prompt(payload: dict[str, Any]) -> list[dict[str, str]]:
    return _request("narrative", NarrativeSpec, payload, ("Create a coherent story arc with at least two beats.",))


def outline_prompt(payload: dict[str, Any]) -> list[dict[str, str]]:
    return _request("outline", OutlineDraft, payload, ("Return exactly the requested number of slides. The service assigns slide IDs.",))


def sample_prompt(payload: dict[str, Any]) -> list[dict[str, str]]:
    return _request("sample", SampleSpec, payload, (
        "Create only the selected representative slides and one reusable theme token object.",
        "Allowed asset IDs are exactly task_brief.resource_manifest[].resource_id; when that list is empty, do not create image blocks and return empty asset_refs and shared_assets.",
    ))


def slide_batch_prompt(payload: dict[str, Any]) -> list[dict[str, str]]:
    return _request("deck batch", SlideBatchSpec, payload, (
        "Create exactly the requested slide IDs in the supplied order and reuse the frozen theme and layout families.",
        "Allowed asset IDs are exactly allowed_asset_refs; when that list is empty, do not create image blocks and return empty asset_refs.",
    ))
