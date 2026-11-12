"""Contract error envelope: {"error": {"code", "message", "request_id"}}.

Validation errors deliberately omit the rejected input so passwords and tokens are never echoed.
"""
import logging
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

audit_log = logging.getLogger("app.audit")

_REDACT = re.compile(r"(?i)(password|passwd|secret|token|authorization|cookie|csrf|api[_-]?key)(['\"]?\s*[:=]\s*)\S+")


class RedactingFilter(logging.Filter):
    """Last line of defence: mask credential-looking key/value pairs in any app log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _REDACT.sub(r"\1\2[REDACTED]", record.getMessage())
        record.args = ()
        return True


def install_log_redaction() -> None:
    """Attach the redaction filter to the audit logger once (idempotent across create_app calls)."""
    for logger in (audit_log, logging.getLogger("app.provider")):
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(RedactingFilter())


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None,
                 details: list[dict] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        self.details = details


def _envelope(request: Request, status_code: int, code: str, message: str, headers=None, details=None):
    body = {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status_code, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _envelope(request, exc.status_code, exc.code, exc.message, exc.headers, exc.details)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Log the exception type and request id only: messages/tracebacks can contain user data.
        audit_log.error("unhandled_exception type=%s request_id=%s", type(exc).__name__,
                        getattr(request.state, "request_id", None))
        return _envelope(request, 500, "internal_error", "Internal server error.")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _envelope(request, exc.status_code, code, "Request could not be processed.")

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = [
            {"loc": [str(part) for part in err.get("loc", ())], "type": err.get("type"), "message": err.get("msg")}
            for err in exc.errors()
        ]
        return _envelope(request, 422, "validation_error", "Request validation failed.", details=jsonable_encoder(details))


def new_request_id() -> str:
    """Always server-generated; a client-supplied value is never trusted or logged."""
    return uuid.uuid4().hex
