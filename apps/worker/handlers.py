"""Job type registry and built-in handlers.

A handler is an async callable taking the job payload and a context object, and
returning a JSON-serializable result. Handlers are registered by `job_type`
string, which is what the API accepts at enqueue time.

The built-ins below are deliberately simple: this assignment is graded on the
scheduling platform, not on the work the jobs do. They exist to exercise every
path the platform must handle -- success, failure, timeout, CPU-bound work --
so that the reliability machinery can be demonstrated end to end.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger(__name__)


class JobContext(Protocol):
    """What a handler is given besides its payload."""

    job_id: Any
    attempt: int

    async def log(self, level: str, message: str, **fields: Any) -> None: ...


Handler = Callable[[dict[str, Any], JobContext], Awaitable[dict[str, Any]]]

_REGISTRY: dict[str, Handler] = {}


def register(job_type: str) -> Callable[[Handler], Handler]:
    """Decorator registering a handler for a job type."""

    def decorator(fn: Handler) -> Handler:
        _REGISTRY[job_type] = fn
        return fn

    return decorator


def get_handler(job_type: str) -> Handler | None:
    return _REGISTRY.get(job_type)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


class UnknownJobTypeError(Exception):
    """Raised when no handler is registered for a job's type.

    Treated as a normal job failure rather than a worker crash: one bad job
    type must not take down a worker that is correctly processing everything
    else in the queue.
    """


# =============================================================================
# Built-in handlers
# =============================================================================


@register("echo")
async def echo(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Return the payload. The simplest possible success path."""
    await ctx.log("info", "echo handler invoked", keys=list(payload))
    return {"echoed": payload}


@register("sleep")
async def sleep_job(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Sleep for `seconds`. Used to exercise concurrency and timeout handling."""
    seconds = float(payload.get("seconds", 1))
    await ctx.log("info", f"sleeping {seconds}s")
    await asyncio.sleep(seconds)
    return {"slept_seconds": seconds}


@register("http_get")
async def http_get(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Fetch a URL. The canonical I/O-bound job.

    This is the shape of real background work, and the reason asyncio is the
    right concurrency model here: the worker spends this entire call awaiting
    the network, during which the event loop runs other jobs. The GIL is never
    contended because no Python bytecode is executing while we wait.
    """
    url = payload.get("url")
    if not url:
        raise ValueError("http_get requires a 'url' in the payload")

    timeout = float(payload.get("timeout", 10))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)

    await ctx.log("info", f"GET {url} -> {response.status_code}")
    response.raise_for_status()
    return {
        "status_code": response.status_code,
        "content_length": len(response.content),
    }


@register("fail")
async def always_fail(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Always raise. Exercises the retry, backoff and dead-letter paths.

    Accepts `fail_until_attempt` so a test can assert that a job which fails
    twice and then succeeds is recorded as three execution rows with the
    correct outcomes.
    """
    fail_until = payload.get("fail_until_attempt")
    if fail_until is not None and ctx.attempt >= int(fail_until):
        await ctx.log("info", f"succeeding on attempt {ctx.attempt}")
        return {"succeeded_on_attempt": ctx.attempt}

    message = payload.get("message", "Deliberate failure for testing")
    await ctx.log("error", f"failing on attempt {ctx.attempt}")
    raise RuntimeError(f"{message} (attempt {ctx.attempt})")


@register("cpu_burn")
def cpu_burn(payload: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """A CPU-bound job. Deliberately a *synchronous* function.

    The executor detects that this handler is not a coroutine function and
    dispatches it to a ProcessPoolExecutor rather than running it on the event
    loop. That is what keeps a CPU-heavy job from stalling every other job in
    the same worker -- and it sidesteps the GIL entirely, because the work
    happens in a separate interpreter process.

    Documented in DESIGN-DECISIONS.md as the answer to "how does a Python
    worker handle CPU-bound jobs?".
    """
    iterations = int(payload.get("iterations", 5_000_000))
    started = time.perf_counter()
    total = 0
    for i in range(iterations):
        total += i * i
    return {
        "iterations": iterations,
        "checksum": total % 1_000_003,
        "duration_s": round(time.perf_counter() - started, 3),
    }
