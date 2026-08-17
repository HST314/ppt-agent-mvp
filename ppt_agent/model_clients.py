from __future__ import annotations

import hashlib
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from .config import ModelConfig
from .errors import GatewayError, GatewayUnknownResult


@dataclass(frozen=True)
class ModelToolCall:
    name: str
    arguments: str
    call_id: str


@dataclass(frozen=True)
class ModelTurn:
    text: str | None
    response_id: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()


class ResponsesModelClient(Protocol):
    def create(self, *, input: Any, tools: list[dict] | None = None, response_schema: dict | None = None, tool_choice: Any = None) -> ModelTurn: ...


class OpenAIResponsesClient:
    """Narrow Responses API adapter with one bounded timeout retry.

    `structured_output` controls how stage schemas reach the provider:
    - ``json_schema``: always send ``text.format`` (strict provider enforcement).
    - ``prompt``: never send it; the runtime prompt contract plus local
      validation carry the whole burden.
    - ``auto`` (default): send it and fall back only when the provider
      explicitly says that the structured-output parameter itself is not
      supported.  Invalid schemas and unrelated 400 responses fail closed and
      never poison the process-wide capability cache.

    A completed response with neither text nor tool calls is a known,
    side-effect-free outcome (the provider finished processing and returned
    nothing); it is retried at most twice within the same call before being
    surfaced as an error.
    """

    def __init__(self, config: ModelConfig, *, sdk_client=None):
        self.config = config
        self.structured_output = getattr(config, "structured_output", "auto") or "auto"
        self._text_format_unsupported = False
        self._client = sdk_client
        self.last_provider_call_count = 0

    def _request_timeout(self) -> float:
        return getattr(self.config, "request_timeout_seconds", getattr(self.config, "timeout_seconds", 60))

    def _request_client(self):
        return self._client or OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self._request_timeout(),
            max_retries=0,
        )

    supports_execution_cancellation = True

    def create(self, *, input: Any, tools: list[dict] | None = None, response_schema: dict | None = None, tool_choice: Any = None, timeout_seconds: float | None = None, provider_call_limit: int | None = None) -> ModelTurn:
        from .execution import cancellation_state, checkpoint

        use_format = bool(response_schema) and self.structured_output != "prompt" and not self._text_format_unsupported
        empty_attempts = 0
        timeout_attempts = 0
        provider_calls = 0
        self.last_provider_call_count = 0
        while True:
            if provider_call_limit is not None and provider_calls >= provider_call_limit:
                raise GatewayError(
                    "模型阶段真实请求次数已达到上限",
                    audit_details={"category": "provider_call_limit", "retryable": False},
                )
            request: dict[str, Any] = {"model": self.config.model, "input": input}
            if tools:
                request["tools"] = tools
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
            if use_format:
                request["text"] = {"format": {"type": "json_schema", **response_schema}}
            request["timeout"] = max(.001, timeout_seconds if timeout_seconds is not None else self._request_timeout())
            request_client = self._request_client()
            cancelled, deadline = cancellation_state()
            monitor_stop = threading.Event()

            def abort_on_cancel():
                while not monitor_stop.wait(.01):
                    if (cancelled is not None and cancelled()) or (deadline is not None and time.monotonic() >= deadline):
                        close = getattr(request_client, "close", None)
                        if close is not None:
                            close()
                        return

            monitor = threading.Thread(target=abort_on_cancel, name="ppt-model-cancellation", daemon=False)
            monitor.start()
            try:
                provider_calls += 1
                self.last_provider_call_count = provider_calls
                response = request_client.responses.create(**request)
            except (APITimeoutError, TimeoutError, socket.timeout) as exc:
                checkpoint()
                timeout_attempts += 1
                if timeout_attempts <= 1:
                    continue
                raise self._transport_error(exc, "timeout", attempts=timeout_attempts) from exc
            except APIStatusError as exc:
                checkpoint()
                format_rejection = self._format_rejection(exc) if use_format else None
                if self.structured_output == "auto" and format_rejection == "unsupported_parameter":
                    self._text_format_unsupported = True
                    use_format = False
                    continue
                raise self._status_error(exc) from exc
            except (APIConnectionError, ConnectionError, OSError) as exc:
                checkpoint()
                raise self._transport_error(exc, "connection") from exc
            except Exception as exc:
                checkpoint()
                raise GatewayError(
                    "模型 SDK 返回了无法分类的失败，请联系管理员核对运行日志",
                    retryable=False,
                    audit_details={
                        "category": "sdk_error",
                        "sdk_exception_type": type(exc).__name__,
                        "retryable": False,
                    },
                ) from exc
            finally:
                monitor_stop.set()
                monitor.join()
                if self._client is None:
                    request_client.close()
            checkpoint()
            text = getattr(response, "output_text", None)
            calls = []
            for item in getattr(response, "output", ()) or ():
                if getattr(item, "type", None) == "function_call":
                    name, arguments, call_id = getattr(item, "name", None), getattr(item, "arguments", None), getattr(item, "call_id", None)
                    if not all(isinstance(value, str) and value for value in (name, arguments, call_id)):
                        raise GatewayError("模型工具调用契约无效")
                    calls.append(ModelToolCall(name, arguments, call_id))
            if (not isinstance(text, str) or not text.strip()) and not calls:
                # 端点偶发返回空响应（请求已被完整处理、服务端无副作用），在同一
                # 调用内有界重试；仍为空才判失败。
                empty_attempts += 1
                if empty_attempts <= 2:
                    continue
                raise GatewayError(
                    "模型响应缺少文本结果",
                    audit_details={"category": "empty_response", "retryable": False},
                )
            response_id = getattr(response, "id", None)
            return ModelTurn(text=text if isinstance(text, str) else None, response_id=response_id if isinstance(response_id, str) else None, tool_calls=tuple(calls))

    @staticmethod
    def _request_id_hash(exc: Exception) -> str | None:
        request_id = getattr(exc, "request_id", None)
        response = getattr(exc, "response", None)
        if not request_id and response is not None:
            request_id = getattr(response, "headers", {}).get("x-request-id")
        if not isinstance(request_id, str) or not request_id:
            return None
        return hashlib.sha256(request_id.encode()).hexdigest()

    @staticmethod
    def _retry_after(exc: Exception) -> int | None:
        response = getattr(exc, "response", None)
        value = getattr(response, "headers", {}).get("retry-after") if response is not None else None
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return None
        return seconds if 0 <= seconds <= 86400 else None

    @classmethod
    def _format_rejection(cls, exc: Exception) -> str | None:
        """Classify a provider 400 without exposing the provider response.

        Only an explicit rejection of ``text.format``/structured outputs may
        enable prompt-only fallback.  A malformed JSON Schema is a deployment
        error, not evidence that the endpoint lacks the capability.
        """
        if int(getattr(exc, "status_code", 0) or 0) != 400:
            return None

        fragments = [str(exc)]

        def collect(value: Any, depth: int = 0) -> None:
            if depth > 4:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    fragments.append(str(key))
                    collect(item, depth + 1)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect(item, depth + 1)
            elif isinstance(value, (str, int)):
                fragments.append(str(value))

        collect(getattr(exc, "body", None))
        text = " ".join(fragments).lower().replace("_", " ")
        schema_markers = (
            "invalid schema",
            "schema is invalid",
            "invalid json schema",
            "schema for response format",
            "json schema for response format",
            "not permitted in schema",
        )
        if any(marker in text for marker in schema_markers):
            return "invalid_schema"

        format_markers = ("text.format", "text format", "response format", "structured output", "json schema")
        unsupported_markers = (
            "unsupported parameter",
            "unknown parameter",
            "unrecognized request argument",
            "not supported",
            "does not support",
        )
        if any(marker in text for marker in format_markers) and any(marker in text for marker in unsupported_markers):
            return "unsupported_parameter"
        return None

    @classmethod
    def _status_error(cls, exc: Exception) -> GatewayError:
        status = int(getattr(exc, "status_code", 0) or 0)
        format_rejection = cls._format_rejection(exc)
        mapping = {
            400: ("model_request_invalid", "模型请求与当前端点不兼容，请联系管理员检查模型与结构化输出配置", False, "request_invalid"),
            401: ("model_authentication_failed", "模型服务认证失败，请联系管理员检查凭据", False, "authentication"),
            403: ("model_permission_denied", "模型服务拒绝访问，请联系管理员检查权限", False, "permission"),
            404: ("model_not_found", "配置的模型或端点不存在，请联系管理员检查配置", False, "not_found"),
            429: ("model_rate_limited", "模型服务请求过于频繁，请等待后重新探测", True, "rate_limit"),
        }
        if status == 400 and format_rejection == "invalid_schema":
            code, message, retryable, category = (
                "model_schema_invalid",
                "结构化输出 Schema 不受当前模型支持，请联系管理员检查提供商 Schema 配置",
                False,
                "schema_invalid",
            )
        elif status in mapping:
            code, message, retryable, category = mapping[status]
        elif 500 <= status <= 599:
            code, message, retryable, category = (
                "model_upstream_unavailable",
                "模型服务暂时不可用，请稍后重新探测",
                True,
                "upstream",
            )
        else:
            code, message, retryable, category = (
                "gateway_error",
                "模型服务返回了无法分类的状态，请联系管理员核对运行日志",
                False,
                "http_status",
            )
        return GatewayError(
            message,
            code=code,
            status=503 if status in {401, 403, 404, 429} or status >= 500 else 502,
            retryable=retryable,
            retry_after_seconds=cls._retry_after(exc),
            audit_details={
                "category": category,
                "http_status": status or None,
                "sdk_exception_type": type(exc).__name__,
                "provider_request_id_sha256": cls._request_id_hash(exc),
                "retryable": retryable,
            },
        )

    @staticmethod
    def _transport_error(exc: Exception, category: str, *, attempts: int = 1) -> GatewayUnknownResult:
        timeout = category == "timeout"
        return GatewayUnknownResult(
            "模型请求超时，结果可能未知；请先核对供应商记录" if timeout else "模型连接中断，结果可能未知；请先核对供应商记录",
            code="model_timeout" if timeout else "model_connection_error",
            retryable=False,
            audit_details={
                "category": category,
                "sdk_exception_type": type(exc).__name__,
                "attempts": attempts,
                "retryable": False,
            },
        )


class FakeResponsesClient:
    def __init__(self, text: str = "fake-response"):
        self.text = text

    def create(self, *, input: Any, tools: list[dict] | None = None, response_schema: dict | None = None, tool_choice: Any = None) -> ModelTurn:
        return ModelTurn(self.text, "fake-response-id")


def model_clients_from_config(config):
    if config.mode == "fake":
        return {}
    return {
        "generation": OpenAIResponsesClient(config.generation),
        "inspection": OpenAIResponsesClient(config.inspection),
    }
