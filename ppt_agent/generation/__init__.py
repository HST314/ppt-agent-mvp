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
from .model_gateway import ModelGateway
from .pipeline import FileCheckpointStore, GenerationPipeline, StageResult

__all__ = [
    "CONTRACT_VERSION",
    "DeckSpec",
    "FileCheckpointStore",
    "GenerationPipeline",
    "ModelGateway",
    "NarrativeSpec",
    "OutlineSpec",
    "SlideSpec",
    "StageResult",
    "TaskBrief",
    "ThemeTokens",
]
