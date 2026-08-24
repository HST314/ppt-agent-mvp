from __future__ import annotations

from typing import Any

from .contracts import (
    CONTRACT_VERSION,
    HtmlDeckBatchSpec,
    HtmlSampleSpec,
    NarrativeSpec,
    OutlineDraft,
    SampleSpec,
    SlideBatchSpec,
    canonical_json,
)


PROMPT_VERSION = "2.0"


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


def _html_request(stage: str, contract, payload: dict[str, Any], rules: tuple[str, ...]) -> list[dict[str, str]]:
    system = "\n".join((
        f"You are the presentation design Agent for the {stage} stage.",
        "Read the active Skill progressively, then return exactly one JSON object matching the supplied JSON Schema.",
        "The JSON transport shell is structural; the visual design itself must be expressed as HTML fragments and CSS.",
        "Each html_fragment must be one complete section.slide whose id and data-slide-id equal the supplied slide_id.",
        "Do not return html/head/body wrappers, scripts, event handlers, remote URLs, workflow state, or prose outside JSON.",
        "Use resources only through the supplied resources:// resource IDs and declare every used ID in asset_refs.",
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


def html_sample_prompt(payload: dict[str, Any]) -> list[dict[str, str]]:
    return _html_request("sample", HtmlSampleSpec, payload, (
        "Generate only selected_slides, in exactly that order, as autonomous HTML/CSS compositions.",
        "Return a reusable shared_css design system, a concrete design_intent, and optional page-scoped slide_css.",
        "Use the complete generation_context, narrative, outline, source materials, and allowed_assets supplied by the service.",
    ))


def html_deck_batch_prompt(payload: dict[str, Any]) -> list[dict[str, str]]:
    return _html_request("deck batch", HtmlDeckBatchSpec, payload, (
        "Generate only requested_slides, in exactly that order; do not regenerate confirmed sample pages.",
        "Return frozen_shared_css and frozen_design_intent byte-for-byte as shared_css and design_intent.",
        "Keep the confirmed sample's visual language while choosing a page composition appropriate to each slide's content.",
    ))
