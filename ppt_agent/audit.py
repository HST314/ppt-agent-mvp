from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar


_AGENT_AUDIT_CONTEXT: ContextVar[dict[str, str]] = ContextVar("agent_audit_context", default={})


def current_agent_audit_context() -> dict[str, str]:
    return dict(_AGENT_AUDIT_CONTEXT.get())


@contextmanager
def bind_agent_audit_context(*, task_id: str, job_id: str | None = None):
    value = {"task_id": task_id}
    if job_id:
        value["job_id"] = job_id
    token = _AGENT_AUDIT_CONTEXT.set(value)
    try:
        yield
    finally:
        _AGENT_AUDIT_CONTEXT.reset(token)
