from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import DomainError, NotFoundError, ValidationError


def error_response(error: DomainError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content=error.public())


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def handle_domain_error(_request: Request, error: DomainError) -> JSONResponse:
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return error_response(ValidationError("请求字段无效"))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        if error.status_code == 404:
            return error_response(NotFoundError("接口不存在"))
        public = DomainError("请求方法不支持" if error.status_code == 405 else "HTTP 请求失败")
        public.code = "method_not_allowed" if error.status_code == 405 else "http_error"
        public.status = error.status_code
        return error_response(public)

    @app.exception_handler(Exception)
    async def handle_unknown(request: Request, error: Exception) -> JSONResponse:
        diagnostic_id = uuid.uuid4().hex
        logging.exception(
            "unhandled FastAPI request error",
            extra={"diagnostic_id": diagnostic_id, "path": request.url.path},
        )
        public = DomainError("请求处理失败")
        public.code = "internal_error"
        public.status = 500
        public.diagnostic_id = diagnostic_id
        return error_response(public)
