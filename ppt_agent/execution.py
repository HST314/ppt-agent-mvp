from __future__ import annotations
import contextvars, time
from contextlib import contextmanager
from .errors import ConflictError, GatewayError

_cancelled = contextvars.ContextVar("ppt_agent_cancelled", default=None)
_deadline = contextvars.ContextVar("ppt_agent_deadline", default=None)

class ExecutionCancelled(ConflictError):
    def __init__(self): super().__init__("后台任务已取消")

class ExecutionDeadlineExceeded(GatewayError):
    def __init__(self): super().__init__("阶段执行超过硬截止时间")

@contextmanager
def execution_scope(cancelled, deadline):
    ct, dt = _cancelled.set(cancelled), _deadline.set(deadline)
    try: yield
    finally: _deadline.reset(dt); _cancelled.reset(ct)

def checkpoint():
    cancelled, deadline = _cancelled.get(), _deadline.get()
    if cancelled is not None and cancelled(): raise ExecutionCancelled()
    if deadline is not None and time.monotonic() >= deadline: raise ExecutionDeadlineExceeded()
