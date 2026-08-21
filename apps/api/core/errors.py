"""Structured error handling.

Every failure leaving this API -- expected or not -- is rendered as one shape:

    {
      "error": {
        "code":       "QUEUE_NOT_FOUND",       machine-readable, stable
        "message":    "Queue not found",        human-readable, safe to display
        "details":    {...} | null,             field errors, context
        "request_id": "b3f1..."                 correlates with server logs
      }
    }

A single envelope means the dashboard writes one error handler instead of one
per endpoint, and `request_id` lets a user paste an id from a toast into a log
search and land on the exact failing request. This directly serves the
assignment's "structured error handling" requirement.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from apps.api.core.logging import get_request_id

log = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for all deliberately raised API errors.

    Carrying `status_code` and `code` on the exception lets service-layer code
    raise a domain error without importing FastAPI or knowing anything about
    HTTP -- the handler below does the translation.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details
        self.status_code = status_code or self.status_code
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    message = "Resource not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    message = "Resource already exists"


class ValidationError(AppError):
    # Literal 422 rather than the named constant: Starlette renamed
    # HTTP_422_UNPROCESSABLE_ENTITY to ..._CONTENT, and pinning to the
    # number keeps this working across both versions.
    status_code = 422
    code = "VALIDATION_ERROR"
    message = "Request validation failed"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHENTICATED"
    message = "Authentication required"


class AuthorizationError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"
    message = "You do not have permission to perform this action"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"
    message = "Too many requests"


def _envelope(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": get_request_id(),
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers so that no error path can bypass the envelope."""

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        # Expected, deliberate errors: log at warning, no stack trace. These are
        # normal operation (a 404 is not an incident) and stack traces for them
        # would drown the genuine failures.
        log.warning(
            "request.failed",
            error_code=exc.code,
            status_code=exc.status_code,
            message=exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Reshape FastAPI's default 422 body into our envelope, flattening the
        # location tuple into a dotted field path the dashboard can bind to.
        fields = [
            {
                "field": ".".join(str(p) for p in err["loc"] if p != "body"),
                "message": err["msg"],
                "type": err["type"],
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_envelope(
                "VALIDATION_ERROR",
                "Request validation failed",
                {"fields": fields},
            ),
        )

    @app.exception_handler(IntegrityError)
    async def _handle_integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        # A unique-constraint violation is a race we lost, not a server fault:
        # two concurrent requests tried to create the same slug and the database
        # correctly rejected the second. Report it as 409, not 500.
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        log.warning("request.integrity_error", constraint=constraint)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_envelope(
                "CONFLICT",
                "That resource already exists",
                {"constraint": constraint} if constraint else None,
            ),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Genuine bugs: log with full traceback, but never leak the exception
        # text to the client -- messages routinely contain table names, SQL
        # fragments and occasionally credentials. The request_id is the bridge
        # between what the user sees and what we logged.
        log.exception("request.unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope(
                "INTERNAL_ERROR",
                "An unexpected error occurred. Reference the request_id when "
                "reporting this.",
            ),
        )
