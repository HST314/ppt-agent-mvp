from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any, Protocol

from openai import APIConnectionError, APITimeoutError, OpenAI

from .config import ModelConfig
from .errors import GatewayError, GatewayUnknownResult


@dataclass(frozen=True)
class ModelTurn:
    text: str
    response_id: str | None = None


class ResponsesModelClient(Protocol):
    def create(self, *, input: Any, tools: list[dict] | None = None, response_schema: dict | None = None) -> ModelTurn: ...


class OpenAIResponsesClient:
    """Narrow Responses API adapter. Unknown outcomes are never retried here."""

    def __init__(self, config: ModelConfig, *, sdk_client=None):
        self.config = config
        self._client = sdk_client or OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=config.timeout_seconds, max_retries=0)

    def create(self, *, input: Any, tools: list[dict] | None = None, response_schema: dict | None = None) -> ModelTurn:
        request: dict[str, Any] = {"model": self.config.model, "input": input}
        if tools:
            request["tools"] = tools
        if response_schema:
            request["text"] = {"format": {"type": "json_schema", **response_schema}}
        try:
            response = self._client.responses.create(**request)
        except (APITimeoutError, APIConnectionError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            raise GatewayUnknownResult("模型调用结果未知，请人工确认后再重试") from exc
        except Exception as exc:
            raise GatewayError("模型服务调用失败") from exc
        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text.strip():
            raise GatewayError("模型响应缺少文本结果")
        response_id = getattr(response, "id", None)
        return ModelTurn(text=text, response_id=response_id if isinstance(response_id, str) else None)


class FakeResponsesClient:
    def __init__(self, text: str = "fake-response"):
        self.text = text

    def create(self, *, input: Any, tools: list[dict] | None = None, response_schema: dict | None = None) -> ModelTurn:
        return ModelTurn(self.text, "fake-response-id")


def model_clients_from_config(config):
    if config.mode == "fake":
        return {}
    return {
        "generation": OpenAIResponsesClient(config.generation),
        "inspection": OpenAIResponsesClient(config.inspection),
    }
