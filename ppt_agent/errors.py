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

    def public(self) -> dict:
        payload = super().public()
        audit_id = getattr(self, "agent_audit_id", None)
        if audit_id:
            payload["error"]["agent_audit_id"] = audit_id
        return payload

class GatewayUnknownResult(GatewayError):
    code = "gateway_unknown_result"
    status = 503
