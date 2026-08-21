"""Structured JSON logging with per-request correlation.

Every log line carries a `request_id`. In a system with four process types
writing to one database, "which request caused this?" is otherwise unanswerable
-- and the same id is returned in error responses, so a user-visible failure can
be traced to its server-side log line directly.

JSON in production (machine-parseable for any log aggregator), coloured
key-value in development (readable in a terminal).
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextvars import ContextVar

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ContextVar, not a global: asyncio runs many requests concurrently in one
# process, and a plain module-level variable would be overwritten by whichever
# request ran most recently. ContextVars are isolated per async task.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str) -> None:
    _request_id.set(value)


def _inject_request_id(_logger, _method, event_dict):
    """structlog processor: stamp every event with the current request id."""
    event_dict["request_id"] = _request_id.get()
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure structlog and route stdlib logging through it.

    The stdlib LoggerFactory is used rather than structlog's PrintLogger so that
    `add_logger_name` has a real logger to read a name from, and so that output
    from libraries that log through stdlib (uvicorn, alembic, sqlalchemy) shares
    one destination and format with our own events.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # structlog renders the full line; stdlib just emits it verbatim.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level)

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            _inject_request_id,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Quiet the libraries that log a line per connection or per request. Our
    # own RequestContextMiddleware already emits one structured line per
    # request, so uvicorn's access log would be a duplicate in a second format.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, time the request, and log one line per request.

    An inbound `X-Request-ID` is honoured rather than replaced, so a trace
    started by the dashboard (or by a future gateway) stays continuous across
    service boundaries. The id is echoed back in the response header.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        set_request_id(request_id)

        log = structlog.get_logger("api.request")
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # Log the timing even for failures; the exception handler will
            # produce the response body.
            duration_ms = (time.perf_counter() - started) * 1000
            log.exception(
                "http.request",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id

        # Client errors are the caller's problem, server errors are ours --
        # log them at different levels so alerting can key off severity.
        level = "warning" if response.status_code >= 400 else "info"
        getattr(log, level)(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response
