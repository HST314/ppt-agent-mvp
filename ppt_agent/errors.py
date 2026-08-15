from __future__ import annotations

import uuid


class DomainError(Exception):
    code = "domain_error"
    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.diagnostic_id = uuid.uuid4().hex

    def public(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "diagnostic_id": self.diagnostic_id}}


class ValidationError(DomainError):
    code = "validation_error"


class ConflictError(DomainError):
    code = "conflict"
    status = 409


class NotFoundError(DomainError):
    code = "not_found"
    status = 404


class GateError(ConflictError):
    code = "gate_not_satisfied"


class GatewayError(DomainError):
    code = "gateway_error"
    status = 502

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        audit_details: dict | None = None,
    ):
        super().__init__(message)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.retryable = bool(retryable)
        self.retry_after_seconds = retry_after_seconds
        self.audit_details = dict(audit_details or {})

    def public(self) -> dict:
        payload = super().public()
        payload["error"]["retryable"] = self.retryable
        if self.retry_after_seconds is not None:
            payload["error"]["retry_after_seconds"] = self.retry_after_seconds
        audit_id = getattr(self, "agent_audit_id", None)
        if audit_id:
            payload["error"]["agent_audit_id"] = audit_id
        return payload

    def safe_audit_details(self) -> dict:
        return {
            key: value
            for key, value in self.audit_details.items()
            if key in {
                "category",
                "http_status",
                "sdk_exception_type",
                "provider_request_id_sha256",
                "retryable",
            }
            and value is not None
        }


class GatewayUnknownResult(GatewayError):
    code = "gateway_unknown_result"
    status = 503


class RuntimeUnavailableError(DomainError):
    code = "runtime_unavailable"
    status = 503

    def __init__(
        self,
        message: str = "模型运行时尚未就绪，请修复配置并重新探测",
        *,
        runtime_error_code: str | None = None,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        agent_audit_id: str | None = None,
        diagnostic_id: str | None = None,
        probe_id: str | None = None,
        failed_check: str | None = None,
    ):
        super().__init__(message)
        if diagnostic_id:
            self.diagnostic_id = diagnostic_id
        self.runtime_error_code = runtime_error_code
        self.retryable = bool(retryable)
        self.retry_after_seconds = retry_after_seconds
        self.agent_audit_id = agent_audit_id
        self.probe_id = probe_id
        self.failed_check = failed_check

    def public(self) -> dict:
        payload = super().public()
        payload["error"]["retryable"] = self.retryable
        if self.runtime_error_code:
            payload["error"]["runtime_error_code"] = self.runtime_error_code
        if self.retry_after_seconds is not None:
            payload["error"]["retry_after_seconds"] = self.retry_after_seconds
        if self.agent_audit_id:
            payload["error"]["agent_audit_id"] = self.agent_audit_id
        if self.probe_id:
            payload["error"]["probe_id"] = self.probe_id
        if self.failed_check:
            payload["error"]["failed_check"] = self.failed_check
        return payload
