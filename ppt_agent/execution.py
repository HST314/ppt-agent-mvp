from __future__ import annotations
import contextvars, time, threading
from contextlib import contextmanager
from .errors import ConflictError, GatewayError

_cancelled = contextvars.ContextVar("ppt_agent_cancelled", default=None)
_deadline = contextvars.ContextVar("ppt_agent_deadline", default=None)
_progress = contextvars.ContextVar("ppt_agent_progress", default=None)

class ExecutionCancelled(ConflictError):
    def __init__(self): super().__init__("后台任务已取消")

class ExecutionDeadlineExceeded(GatewayError):
    def __init__(self): super().__init__("阶段执行超过硬截止时间")

@contextmanager
def execution_scope(cancelled, deadline, progress=None):
    ct, dt, pt = _cancelled.set(cancelled), _deadline.set(deadline), _progress.set(progress)
    try: yield
    finally: _progress.reset(pt); _deadline.reset(dt); _cancelled.reset(ct)

def checkpoint():
    cancelled, deadline = _cancelled.get(), _deadline.get()
    if cancelled is not None and cancelled(): raise ExecutionCancelled()
    if deadline is not None and time.monotonic() >= deadline: raise ExecutionDeadlineExceeded()

def progress(step, message=None):
    """Publish a real business checkpoint without coupling domain code to Jobs."""
    checkpoint()
    callback = _progress.get()
    if callback is not None: callback(step, message)

def remaining_seconds(default=None):
    deadline = _deadline.get()
    return default if deadline is None else max(0.0, deadline - time.monotonic())

def cancellation_state():
    """Return the current cancellation predicate and absolute deadline."""
    return _cancelled.get(), _deadline.get()

def interruptible(call, *, poll_seconds=.02):
    """Return promptly on cancellation/deadline while a blocking SDK call drains.

    The worker inherits the execution context, so any later domain publication is
    rejected by the task-scoped checkpoint inside the store lock.
    """
    result, done = {}, threading.Event()
    context = contextvars.copy_context()
    def run():
        try: result["value"] = context.run(call)
        except BaseException as exc: result["error"] = exc
        finally: done.set()
    threading.Thread(target=run, name="ppt-interruptible-call", daemon=True).start()
    while not done.wait(poll_seconds): checkpoint()
    checkpoint()
    if "error" in result: raise result["error"]
    return result.get("value")
