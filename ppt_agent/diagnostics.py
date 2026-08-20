from __future__ import annotations

import json
import logging
import re
import traceback
from typing import Iterable


_BEARER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+")
_BEARER_VALUE_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;}\[\]\\\"']+")
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
    r"([\"']?\s*[:=]\s*)"
    r"(?:[\"'](?:\\.|[^\"'])*[\"']|[^\s,;}]+)"
)
_SECRET_FIELD_RE = re.compile(
    r"(?i)^(?:authorization|proxy[_-]?authorization|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|auth[_-]?token|bearer|password|passwd|secret|client[_-]?secret|"
    r"private[_-]?key|credential(?:s)?|token)$"
)
_KNOWN_TOKEN_RE = re.compile(r"(?i)\b(?:sk|ghp|github_pat|bearer)[-_][a-z0-9._-]+")
_URL_RE = re.compile(r"https?://[^\s\]\[()<>{}\"']+")
_SAFE_ATTRIBUTE_ERROR_RE = re.compile(
    r"^(?:'[^'\r\n]{1,80}' object|type object '[^'\r\n]{1,80}') "
    r"has no attribute '[A-Za-z_][A-Za-z0-9_]{0,80}'$"
)


def _redact_url(match: re.Match[str]) -> str:
    value = match.group(0)
    suffix = ""
    while value and value[-1] in ".,;:":
        suffix = value[-1] + suffix
        value = value[:-1]
    authority_and_path = value.split("?", 1)[0].split("#", 1)[0]
    scheme, separator, remainder = authority_and_path.partition("://")
    if separator and "@" in remainder.split("/", 1)[0]:
        remainder = "[REDACTED]@" + remainder.rsplit("@", 1)[1]
        authority_and_path = scheme + separator + remainder
    return authority_and_path + suffix


def redact_diagnostic_text(value: str, *, secrets: Iterable[str] = ()) -> str:
    """Redact credentials while retaining traceback structure and SDK details."""
    redacted = value
    for secret in sorted(
        {item for item in secrets if isinstance(item, str) and item},
        key=len,
        reverse=True,
    ):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _BEARER_VALUE_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _NAMED_SECRET_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _KNOWN_TOKEN_RE.sub("[REDACTED]", redacted)
    return _URL_RE.sub(_redact_url, redacted)


def redact_diagnostic_payload(value: object, *, secrets: Iterable[str] = ()) -> object:
    """Recursively redact every string in a structured diagnostic payload."""
    secret_values = tuple(item for item in secrets if isinstance(item, str) and item)

    def redact(item: object, *, secret_field: bool = False) -> object:
        if isinstance(item, str):
            if secret_field:
                return "[REDACTED]"
            return redact_diagnostic_text(item, secrets=secret_values)
        if isinstance(item, dict):
            redacted: dict[object, object] = {}
            for key, nested in item.items():
                safe_key = redact_diagnostic_text(key, secrets=secret_values) if isinstance(key, str) else key
                named_secret = secret_field or (
                    isinstance(key, str) and bool(_SECRET_FIELD_RE.fullmatch(key))
                )
                redacted[safe_key] = redact(nested, secret_field=named_secret)
            return redacted
        if isinstance(item, list):
            return [redact(nested, secret_field=secret_field) for nested in item]
        if isinstance(item, tuple):
            return tuple(redact(nested, secret_field=secret_field) for nested in item)
        return item

    return redact(value)


def sanitized_exception_chain(error: BaseException, *, secrets: Iterable[str] = ()) -> str:
    """Return a complete causal traceback without locals or source-code lines.

    Provider exceptions can place response bodies in their messages, and source
    lines in an ordinary formatted traceback can contain test payloads or
    credentials. Preserve every exception type and stack frame, but only retain
    messages owned by this package plus the structural ``AttributeError`` that
    motivated this diagnostic path.
    """
    seen: set[int] = set()

    def render(current: BaseException) -> str:
        if id(current) in seen:
            return f"{type(current).__module__}.{type(current).__qualname__}: [cycle]\n"
        seen.add(id(current))
        parts: list[str] = []
        cause = current.__cause__
        context = current.__context__
        if cause is not None:
            parts.append(render(cause))
            parts.append("\nThe above exception was the direct cause of the following exception:\n\n")
        elif context is not None and not current.__suppress_context__:
            parts.append(render(context))
            parts.append("\nDuring handling of the above exception, another exception occurred:\n\n")
        frames = traceback.extract_tb(current.__traceback__) if current.__traceback__ is not None else ()
        if frames:
            parts.append("Traceback (most recent call last):\n")
            for frame in frames:
                parts.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n')
        exception_type = f"{type(current).__module__}.{type(current).__qualname__}"
        owned_message = (
            type(current).__module__.startswith("ppt_agent.")
            and isinstance(getattr(current, "message", None), str)
        )
        raw_message = str(current)
        safe_attribute_message = isinstance(current, AttributeError) and bool(
            _SAFE_ATTRIBUTE_ERROR_RE.fullmatch(raw_message)
        )
        message = raw_message if owned_message or safe_attribute_message else "[message redacted]"
        parts.append(f"{exception_type}: {message}\n")
        return "".join(parts)

    return redact_diagnostic_text(render(error), secrets=secrets)


def log_exception_chain(
    error: BaseException,
    *,
    diagnostic_id: str,
    probe_id: str,
    context: dict | None = None,
    secrets: Iterable[str] = (),
) -> None:
    """Emit one searchable, structured server log for a runtime probe failure."""
    secret_values = tuple(secrets)
    payload = {
        "event": "runtime_probe_exception",
        "diagnostic_id": diagnostic_id,
        "probe_id": probe_id,
        **dict(context or {}),
        "exception_chain": sanitized_exception_chain(error, secrets=secret_values),
    }
    safe_payload = redact_diagnostic_payload(payload, secrets=secret_values)
    logging.getLogger("ppt_agent.runtime").error(json.dumps(safe_payload, ensure_ascii=False))
