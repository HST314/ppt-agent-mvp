from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, TypeVar

from .contracts import CONTRACT_VERSION, Contract, canonical_json
from .errors import ErrorContext, ModelOutputError, ModelResultUnknown, ModelTransportError


SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|bearer|password|secret|token|credential|cookie)", re.I)
SECRET_TEXT = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|api[_-]?key\s*[:=]\s*)[^\s,;\"']+"
)


def redact(value: Any, *, secret_values: tuple[str, ...] = ()) -> Any:
    """Return JSON-shaped diagnostics without credentials or raw exceptions."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if SECRET_KEY.search(str(key)) else redact(item, secret_values=secret_values)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item, secret_values=secret_values) for item in value]
    if isinstance(value, str):
        result = SECRET_TEXT.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
        for secret in secret_values:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


@dataclass(frozen=True)
class ProviderResponse:
    response_id: str
    output: Any
    status: str = "completed"


class StructuredProvider(Protocol):
    def create(self, **request: Any) -> Any: ...


@dataclass(frozen=True)
class GatewayResult:
    contract: Contract
    response_id_sha256: str
    provider_calls: int
    recovery_count: int
    model: str
    elapsed_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


ContractType = TypeVar("ContractType", bound=Contract)


class ModelGateway:
    """The only model boundary used by the rebuild pipeline.

    Provider JSON Schema is mandatory. A local contract parse is performed on
    every created or recovered result before the value can leave this class.
    Only a transport failure explicitly marked as pre-dispatch can be replayed.
    If a stable response ID is available, retrieval is attempted before any
    replay decision.
    """

    def __init__(
        self,
        provider: StructuredProvider,
        *,
        model: str,
        timeout_seconds: float = 120.0,
        max_pre_dispatch_retries: int = 1,
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
        secret_values: tuple[str, ...] = (),
    ):
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_pre_dispatch_retries not in {0, 1}:
            raise ValueError("max_pre_dispatch_retries must be 0 or 1")
        self.provider = provider
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self.max_pre_dispatch_retries = max_pre_dispatch_retries
        self.audit_sink = audit_sink
        self.secret_values = secret_values
        self._guard = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}
        self._completed: dict[str, GatewayResult] = {}

    def generate(
        self,
        contract_type: type[ContractType],
        *,
        input: Any,
        idempotency_key: str,
        stage: str,
    ) -> GatewayResult:
        if not idempotency_key or len(idempotency_key) > 512:
            raise ValueError("idempotency_key must be a bounded non-empty string")
        lock = self._key_lock(idempotency_key)
        with lock:
            with self._guard:
                cached = self._completed.get(idempotency_key)
            if cached is not None:
                if not isinstance(cached.contract, contract_type):
                    raise ModelOutputError("幂等键已绑定其他输出契约", context=ErrorContext(stage=stage))
                return cached
            return self._generate_once(contract_type, input=input, idempotency_key=idempotency_key, stage=stage)

    def discard(self, idempotency_key: str) -> None:
        """Remove a locally completed candidate rejected by downstream validation."""

        with self._guard:
            self._completed.pop(idempotency_key, None)

    def _generate_once(
        self,
        contract_type: type[ContractType],
        *,
        input: Any,
        idempotency_key: str,
        stage: str,
    ) -> GatewayResult:
        started = time.monotonic()
        provider_calls = 0
        recovery_count = 0
        pre_dispatch_retries = 0
        schema_corrections = 0
        request_input = input
        while True:
            provider_calls += 1
            try:
                raw = self._provider_create(
                    model=self.model,
                    input=request_input,
                    response_schema=contract_type.provider_schema(),
                    timeout_seconds=self.timeout_seconds,
                    idempotency_key=idempotency_key if not schema_corrections else f"{idempotency_key}:schema-correction:{schema_corrections}",
                )
                response = self._normalize_response(raw)
                contract = self._parse_output(contract_type, response.output, stage=stage, response_id=response.response_id)
                result = GatewayResult(
                    contract=contract,
                    response_id_sha256=_hash_identifier(response.response_id),
                    provider_calls=provider_calls,
                    recovery_count=recovery_count,
                    model=self.model,
                    elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                )
                with self._guard:
                    self._completed[idempotency_key] = result
                self._audit(stage, idempotency_key, "succeeded", result=result)
                return result
            except ModelOutputError as exc:
                if schema_corrections == 0:
                    schema_corrections = 1
                    request_input = self._schema_correction_input(input, exc.context.field_path)
                    self._audit(stage, idempotency_key, "schema_correction", exception=exc, provider_calls=provider_calls)
                    continue
                self._audit(stage, idempotency_key, "invalid_output", exception=exc, provider_calls=provider_calls)
                raise
            except (ModelResultUnknown, ModelTransportError):
                raise
            except Exception as exc:
                response_id = self._response_id(exc)
                if response_id is not None and self._can_retrieve():
                    recovered = self._retrieve(response_id)
                    recovery_count += 1
                    if recovered is not None:
                        contract = self._parse_output(contract_type, recovered.output, stage=stage, response_id=recovered.response_id)
                        result = GatewayResult(
                            contract=contract,
                            response_id_sha256=_hash_identifier(recovered.response_id),
                            provider_calls=provider_calls,
                            recovery_count=recovery_count,
                            model=self.model,
                            elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                        )
                        with self._guard:
                            self._completed[idempotency_key] = result
                        self._audit(stage, idempotency_key, "recovered", result=result)
                        return result
                if self._pre_dispatch(exc) and self._transient(exc) and pre_dispatch_retries < self.max_pre_dispatch_retries:
                    pre_dispatch_retries += 1
                    self._audit(stage, idempotency_key, "pre_dispatch_retry", exception=exc, provider_calls=provider_calls)
                    continue
                context = ErrorContext(
                    stage=stage,
                    response_id_sha256=_hash_identifier(response_id) if response_id else None,
                    retryable=self._pre_dispatch(exc) and self._transient(exc),
                )
                known_failure = self._known_failure(exc)
                self._audit(stage, idempotency_key, "rejected" if known_failure else "unknown" if not self._pre_dispatch(exc) else "transport_failed", exception=exc, provider_calls=provider_calls)
                if known_failure:
                    raise ModelTransportError("模型端点明确拒绝了请求", context=context) from exc
                if not self._pre_dispatch(exc):
                    raise ModelResultUnknown("模型请求结果无法证明，已停止重复提交", context=context) from exc
                raise ModelTransportError("模型请求在发送前失败", context=context) from exc

    @staticmethod
    def _schema_correction_input(value: Any, field_path: str | None) -> Any:
        if not isinstance(value, (list, tuple)):
            return value
        guidance = (
            "The previous completed response did not satisfy the local contract"
            + (f" at {field_path}" if field_path else "")
            + ". Return one fresh complete JSON object. Preserve every supplied identifier and order. "
              "asset_refs must exactly equal the asset_ref values of image content blocks, and no asset may be used unless supplied in the input. "
              "Preserve supplied checkpoint IDs, theme tokens, and allowed layout families."
        )
        return [*value, {"role": "system", "content": guidance}]

    def _provider_create(self, **request: Any) -> Any:
        create = self.provider.create
        parameters = inspect.signature(create).parameters
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            return create(**request)
        accepted = {key: value for key, value in request.items() if key in parameters}
        return create(**accepted)

    def _retrieve(self, response_id: str) -> ProviderResponse | None:
        retrieve = getattr(self.provider, "retrieve", None)
        if not callable(retrieve):
            return None
        try:
            raw = retrieve(response_id)
        except Exception:
            return None
        response = self._normalize_response(raw, default_response_id=response_id)
        return response if response.status in {"completed", "succeeded"} else None

    def _can_retrieve(self) -> bool:
        return callable(getattr(self.provider, "retrieve", None))

    @staticmethod
    def _normalize_response(raw: Any, *, default_response_id: str = "") -> ProviderResponse:
        if isinstance(raw, ProviderResponse):
            return raw
        if isinstance(raw, dict):
            response_id = raw.get("response_id") or raw.get("id") or default_response_id
            output = raw.get("output", raw.get("output_text", raw.get("text")))
            status = raw.get("status", "completed")
        else:
            response_id = getattr(raw, "response_id", None) or getattr(raw, "id", None) or default_response_id
            output = getattr(raw, "output", None)
            direct_text = getattr(raw, "output_text", None)
            if direct_text is not None:
                output = direct_text
            elif getattr(raw, "text", None) is not None:
                output = getattr(raw, "text")
            status = getattr(raw, "status", "completed")
        if not isinstance(response_id, str) or not response_id:
            response_id = f"local-{hashlib.sha256(canonical_json(redact(output)).encode()).hexdigest()[:24]}"
        if status not in {"completed", "succeeded"}:
            raise ModelResultUnknown("provider 返回非完成状态", context=ErrorContext(response_id_sha256=_hash_identifier(response_id)))
        return ProviderResponse(response_id=response_id, output=output, status=status)

    @staticmethod
    def _parse_output(contract_type: type[ContractType], output: Any, *, stage: str, response_id: str) -> ContractType:
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError as exc:
                raise ModelOutputError(
                    "provider 输出不是完整 JSON 对象",
                    context=ErrorContext(stage=stage, field_path=f"json:{exc.pos}", response_id_sha256=_hash_identifier(response_id)),
                ) from exc
        try:
            return contract_type.parse(output)
        except Exception as exc:
            if isinstance(exc, ModelOutputError):
                raise
            field_path = getattr(getattr(exc, "context", None), "field_path", None)
            raise ModelOutputError(
                "provider 输出未通过本地契约校验",
                context=ErrorContext(stage=stage, field_path=field_path, response_id_sha256=_hash_identifier(response_id)),
            ) from exc

    @staticmethod
    def _response_id(exc: Exception) -> str | None:
        for name in ("response_id", "provider_response_id", "request_id"):
            value = getattr(exc, name, None)
            if isinstance(value, str) and value:
                return value
        response = getattr(exc, "response", None)
        if response is not None:
            headers = getattr(response, "headers", {}) or {}
            for key in ("x-response-id", "x-request-id", "request-id"):
                value = headers.get(key) if hasattr(headers, "get") else None
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _pre_dispatch(exc: Exception) -> bool:
        if getattr(exc, "request_sent", None) is False or getattr(exc, "replay_safe", None) is True:
            return True
        phase = str(getattr(exc, "transport_phase", "")).lower()
        return phase in {"dns", "connect", "pool"} and getattr(exc, "result_certainty", "unsent") == "unsent"

    @staticmethod
    def _transient(exc: Exception) -> bool:
        if getattr(exc, "retryable", None) is not None:
            return bool(getattr(exc, "retryable"))
        return isinstance(exc, (ConnectionError, TimeoutError, OSError))

    @staticmethod
    def _known_failure(exc: Exception) -> bool:
        details = getattr(exc, "audit_details", {}) or {}
        certainty = details.get("result_certainty") or getattr(exc, "result_certainty", None)
        status = details.get("http_status") or getattr(exc, "status_code", None)
        return certainty in {"known", "failed"} or isinstance(status, int) and 400 <= status < 600

    def _audit(self, stage: str, idempotency_key: str, status: str, *, result: GatewayResult | None = None, exception: Exception | None = None, provider_calls: int | None = None) -> None:
        if self.audit_sink is None:
            return
        record: dict[str, Any] = {
            "schema_version": CONTRACT_VERSION,
            "event": "model_gateway",
            "stage": stage,
            "status": status,
            "model": self.model,
            "idempotency_key_sha256": hashlib.sha256(idempotency_key.encode()).hexdigest(),
        }
        if result is not None:
            record.update({"provider_calls": result.provider_calls, "recovery_count": result.recovery_count, "response_id_sha256": result.response_id_sha256, "elapsed_ms": result.elapsed_ms})
        if provider_calls is not None:
            record["provider_calls"] = provider_calls
        if exception is not None:
            record.update({"exception_type": type(exception).__name__, "retryable": self._pre_dispatch(exception) and self._transient(exception)})
        self.audit_sink(redact(record, secret_values=self.secret_values))

    def _key_lock(self, key: str) -> threading.Lock:
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self._guard:
            return self._locks.setdefault(digest, threading.Lock())


def _hash_identifier(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
