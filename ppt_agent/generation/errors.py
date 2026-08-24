from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorContext:
    stage: str | None = None
    field_path: str | None = None
    response_id_sha256: str | None = None
    retryable: bool = False


class GenerationCoreError(Exception):
    """Base error with a stable public code and secret-free context."""

    code = "generation_core_error"

    def __init__(self, message: str, *, context: ErrorContext | None = None):
        super().__init__(message)
        self.message = message
        self.context = context or ErrorContext()

    def public(self) -> dict[str, Any]:
        details = {
            key: value
            for key, value in {
                "stage": self.context.stage,
                "field_path": self.context.field_path,
                "response_id_sha256": self.context.response_id_sha256,
                "retryable": self.context.retryable,
            }.items()
            if value is not None
        }
        return {"code": self.code, "message": self.message, "details": details}


class ContractValidationError(GenerationCoreError):
    code = "generation_contract_invalid"


class ModelTransportError(GenerationCoreError):
    code = "model_transport_error"


class ModelResultUnknown(GenerationCoreError):
    code = "model_result_unknown"


class ModelOutputError(GenerationCoreError):
    code = "model_output_invalid"


class CheckpointConflict(GenerationCoreError):
    code = "checkpoint_conflict"


class RenderValidationError(GenerationCoreError):
    code = "render_validation_failed"

    def __init__(self, message: str, *, context: ErrorContext | None = None, diagnostics: tuple[dict[str, Any], ...] = ()):
        super().__init__(message, context=context)
        self.diagnostics = diagnostics

    def public(self) -> dict[str, Any]:
        value = super().public()
        if self.diagnostics:
            value["details"]["diagnostics"] = list(self.diagnostics)
        return value


class AssetResolutionError(GenerationCoreError):
    code = "asset_resolution_failed"
