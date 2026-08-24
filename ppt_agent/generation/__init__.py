"""Deterministic, contract-first presentation generation core."""

from .contracts import (
    CONTRACT_VERSION,
    DeckSpec,
    NarrativeSpec,
    OutlineSpec,
    SlideSpec,
    TaskBrief,
    ThemeTokens,
)
from .context import ContextTextSource, GenerationContextV2, build_stage_payload
from .model_gateway import ModelGateway
from .pipeline import FileCheckpointStore, GenerationPipeline, StageResult

__all__ = [
    "CONTRACT_VERSION",
    "DeckSpec",
    "FileCheckpointStore",
    "GenerationPipeline",
    "GenerationContextV2",
    "ContextTextSource",
    "ModelGateway",
    "NarrativeSpec",
    "OutlineSpec",
    "SlideSpec",
    "StageResult",
    "TaskBrief",
    "ThemeTokens",
    "build_stage_payload",
]
