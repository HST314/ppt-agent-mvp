"""Compatibility entry points for the retired WSGI adapter.

The product UI and HTTP API are implemented exclusively by ``ppt_agent.web``.
``App`` remains as a small WSGI test bridge for older domain regressions; it
forwards every request to that same FastAPI application and contains no page,
CSS, or JavaScript implementation of its own. Production startup uses
``serve()``, which runs FastAPI directly with Uvicorn.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from http import HTTPStatus
import uvicorn
from fastapi.testclient import TestClient

from .errors import ValidationError
from .service import TaskService
from .store import WorkspaceStore
from .web import create_app


class App:
    """Deprecated WSGI-shaped bridge backed by the single FastAPI app."""

    def __init__(self, service: TaskService):
        self._service = service
        self._client = None
        self._install(service)

    @property
    def service(self) -> TaskService:
        return self._service

    @service.setter
    def service(self, service: TaskService) -> None:
        self.close()
        self._service = service
        self._install(service)

    def _install(self, service: TaskService) -> None:
        self.asgi_app = create_app(service)
        self._client = TestClient(self.asgi_app, raise_server_exceptions=False)
        self._client.__enter__()

    def close(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            self._client = None
            client.__exit__(None, None, None)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __call__(self, environ, start_response):
        started = time.monotonic()
        diagnostic_id = uuid.uuid4().hex
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        query = environ.get("QUERY_STRING", "")
        target = f"{path}?{query}" if query else path
        status_code = 500
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
            if size < 0 or size > 2 * 1024 * 1024:
                raise ValidationError("请求体超过 2 MiB 限制")
            body = environ.get("wsgi.input").read(size) if size else b""
            headers = {
                key[5:].replace("_", "-"): value
                for key, value in environ.items()
                if key.startswith("HTTP_")
            }
            if environ.get("CONTENT_TYPE"):
                headers["content-type"] = environ["CONTENT_TYPE"]
            elif body:
                headers["content-type"] = "application/json"
            response = self._client.request(method, target, content=body, headers=headers)
            status_code = response.status_code
            phrase = HTTPStatus(status_code).phrase
            response_headers = [(key, value) for key, value in response.headers.items()]
            start_response(f"{status_code} {phrase}", response_headers)
            return [response.content]
        except ValidationError as error:
            status_code = error.status
            payload = json.dumps(error.public(), ensure_ascii=False).encode()
            start_response(
                f"{status_code} {HTTPStatus(status_code).phrase}",
                [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(payload)))],
            )
            return [payload]
        finally:
            action = path
            parts = action.split("/")
            if len(parts) > 3 and parts[1:3] == ["v1", "tasks"]:
                parts[3] = "{task_id}"
                action = "/".join(parts)
            logging.info(json.dumps({
                "event": "action_metric",
                "diagnostic_id": diagnostic_id,
                "action": f"{method} {action}",
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "failed": status_code >= 400,
                "status": status_code,
            }))


def serve(root=".ppt-agent-data", host="127.0.0.1", port=8000, service=None):
    """Run the authoritative FastAPI application."""

    uvicorn.run(create_app(service or TaskService(WorkspaceStore(root))), host=host, port=port)
