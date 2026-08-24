"""Contract-first presentation generation core with Agent HTML production output."""

from .contracts import (
    CONTRACT_VERSION,
    DeckSpec,
    HtmlDeckBatchSpec,
    HtmlDeckSpec,
    HtmlSampleSpec,
    HtmlSlideSpec,
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
    "HtmlDeckBatchSpec",
    "HtmlDeckSpec",
    "HtmlSampleSpec",
    "HtmlSlideSpec",
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
