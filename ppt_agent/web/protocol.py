from __future__ import annotations

import json

from fastapi import Request

from ..errors import ValidationError

MAX_JSON_BYTES = 2 * 1024 * 1024


async def json_body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_JSON_BYTES:
        raise ValidationError("请求体超过 2 MiB 限制")
    try:
        value = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValidationError("请求 JSON 无效") from None
    if not isinstance(value, dict):
        raise ValidationError("请求字段无效")
    return value


def exact(body: dict, allowed: set[str], required: set[str] | None = None) -> None:
    if set(body) - allowed or (required or set()) - set(body):
        raise ValidationError("请求字段无效")
