from __future__ import annotations

import hashlib
import socket
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
    """Narrow Responses API adapter. Unknown outcomes are never retried here."""

    def __init__(self, config: ModelConfig, *, sdk_client=None):
        self.config = config
        self._client = sdk_client or OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=config.timeout_seconds, max_retries=0)

    def create(self, *, input: Any, tools: list[dict] | None = None, response_schema: dict | None = None, tool_choice: Any = None) -> ModelTurn:
        request: dict[str, Any] = {"model": self.config.model, "input": input}
        if tools:
            request["tools"] = tools
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        if response_schema:
            request["text"] = {"format": {"type": "json_schema", **response_schema}}
        try:
            response = self._client.responses.create(**request)
        except (APITimeoutError, TimeoutError, socket.timeout) as exc:
            raise self._transport_error(exc, "timeout") from exc
        except APIStatusError as exc:
            raise self._status_error(exc) from exc
        except (APIConnectionError, ConnectionError, OSError) as exc:
            raise self._transport_error(exc, "connection") from exc
        except Exception as exc:
            raise GatewayError(
                "模型 SDK 返回了无法分类的失败，请联系管理员核对运行日志",
                retryable=False,
                audit_details={
                    "category": "sdk_error",
                    "sdk_exception_type": type(exc).__name__,
                    "retryable": False,
                },
            ) from exc
        text = getattr(response, "output_text", None)
        calls = []
        for item in getattr(response, "output", ()) or ():
            if getattr(item, "type", None) == "function_call":
                name, arguments, call_id = getattr(item, "name", None), getattr(item, "arguments", None), getattr(item, "call_id", None)
                if not all(isinstance(value, str) and value for value in (name, arguments, call_id)):
                    raise GatewayError("模型工具调用契约无效")
                calls.append(ModelToolCall(name, arguments, call_id))
        if (not isinstance(text, str) or not text.strip()) and not calls:
            raise GatewayError("模型响应缺少文本结果")
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
    def _status_error(cls, exc: Exception) -> GatewayError:
        status = int(getattr(exc, "status_code", 0) or 0)
        mapping = {
            400: ("model_request_invalid", "模型请求与当前端点不兼容，请联系管理员检查模型与结构化输出配置", False, "request_invalid"),
            401: ("model_authentication_failed", "模型服务认证失败，请联系管理员检查凭据", False, "authentication"),
            403: ("model_permission_denied", "模型服务拒绝访问，请联系管理员检查权限", False, "permission"),
            404: ("model_not_found", "配置的模型或端点不存在，请联系管理员检查配置", False, "not_found"),
            429: ("model_rate_limited", "模型服务请求过于频繁，请等待后重新探测", True, "rate_limit"),
        }
        if status in mapping:
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
    def _transport_error(exc: Exception, category: str) -> GatewayUnknownResult:
        timeout = category == "timeout"
        return GatewayUnknownResult(
            "模型请求超时，结果可能未知；请先核对供应商记录" if timeout else "模型连接中断，结果可能未知；请先核对供应商记录",
            code="model_timeout" if timeout else "model_connection_error",
            retryable=False,
            audit_details={
                "category": category,
                "sdk_exception_type": type(exc).__name__,
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
