"""Executing a single claimed job and recording the outcome."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import insert, select

from apps.worker.handlers import UnknownJobTypeError, get_handler
from packages.retry import compute_delay_seconds
from packages.db import JobExecution, JobLog, Queue, RetryPolicy, session_scope
from packages.db.claim import complete_job, exhaust_job, mark_running, retry_job
from packages.db.enums import ExecutionStatus, LogLevel

log = structlog.get_logger(__name__)


class JobRunContext:
    """Handed to a handler. Buffers log lines for a single batched insert.

    Writing each log line as it is emitted would mean one INSERT per line on
    the highest-volume table in the schema. Buffering and flushing once per
    execution turns a chatty job's fifty inserts into one.
    """

    def __init__(self, job_id: uuid.UUID, attempt: int) -> None:
        self.job_id = job_id
        self.attempt = attempt
        self.execution_id: uuid.UUID | None = None
        self._lines: list[dict[str, Any]] = []

    async def log(self, level: str, message: str, **fields: Any) -> None:
        self._lines.append(
            {
                "level": LogLevel(level.lower()),
                "message": str(message)[:4000],
                "metadata": fields or None,
                "logged_at": datetime.now(UTC),
            }
        )

    def drain(self) -> list[dict[str, Any]]:
        lines, self._lines = self._lines, []
        return lines


async def _run_handler(
    handler, payload: dict, ctx: JobRunContext, timeout_s: int, pool
) -> dict[str, Any]:
    """Invoke a handler under a timeout, on the loop or in a process pool.

    A synchronous handler is CPU-bound by assumption and is dispatched to a
    ProcessPoolExecutor. Running it inline would block the event loop and stall
    every other job this worker is concurrently executing -- the one failure
    mode people correctly worry about with Python concurrency, and the reason
    the distinction is made explicitly here rather than left to chance.
    """
    if asyncio.iscoroutinefunction(handler):
        return await asyncio.wait_for(handler(payload, ctx), timeout=timeout_s)

    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(pool, handler, payload, ctx), timeout=timeout_s
    )


async def _record_execution(
    db,
    job_id: uuid.UUID,
    attempt: int,
    worker_id: uuid.UUID,
    status: ExecutionStatus,
    started_at: datetime,
    error_message: str | None = None,
    error_stack: str | None = None,
    output: dict | None = None,
    log_lines: list[dict] | None = None,
) -> uuid.UUID:
    """Append the immutable record of one attempt, plus its log lines.

    The execution row is written *after* the attempt finishes rather than
    before, because ExecutionStatus has no "running" value by design -- an
    attempt that never reported an outcome is exactly what the reaper detects
    and records as `lost` on the worker's behalf.
    """
    finished_at = datetime.now(UTC)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)

    result = await db.execute(
        insert(JobExecution)
        .values(
            job_id=job_id,
            attempt_number=attempt,
            worker_id=worker_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            error_message=(error_message or None) and error_message[:8000],
            error_stack=(error_stack or None) and error_stack[:16000],
            output=output,
        )
        .returning(JobExecution.id)
    )
    execution_id = result.scalar_one()

    if log_lines:
        await db.execute(
            insert(JobLog),
            [{**line, "job_id": job_id, "execution_id": execution_id} for line in log_lines],
        )

    return execution_id


async def execute_job(job: dict[str, Any], worker_id: uuid.UUID, pool) -> str:
    """Run one claimed job to completion and record everything.

    Returns a short outcome string for the worker's counters.

    Every database write below is fenced by the job's `lock_token`. If the
    reaper revived this job while we were executing -- because our heartbeat
    lapsed -- the token no longer matches and our writes affect zero rows. We
    detect that and discard our result rather than overwriting the attempt that
    legitimately owns the job now.
    """
    job_id = job["id"]
    lock_token = job["lock_token"]
    attempt = job["attempt"]
    max_attempts = job["max_attempts"]
    job_type = job["job_type"]

    ctx = JobRunContext(job_id, attempt)
    started_at = datetime.now(UTC)

    bound = log.bind(job_id=str(job_id), job_type=job_type, attempt=attempt)

    # claimed -> running. A False return means the claim is no longer ours.
    async with session_scope() as db:
        still_ours = await mark_running(db, job_id, lock_token)
    if not still_ours:
        bound.warning("job.claim_lost_before_start")
        return "lost"

    # ---- run --------------------------------------------------------------
    status: ExecutionStatus
    output: dict | None = None
    error_message: str | None = None
    error_stack: str | None = None

    try:
        handler = get_handler(job_type)
        if handler is None:
            raise UnknownJobTypeError(f"No handler registered for '{job_type}'")

        output = await _run_handler(handler, job["payload"], ctx, job["timeout_s"], pool)
        if not isinstance(output, dict):
            output = {"result": output}
        status = ExecutionStatus.SUCCEEDED
        bound.info("job.succeeded")

    except TimeoutError:
        status = ExecutionStatus.TIMEOUT
        error_message = f"Job exceeded its {job['timeout_s']}s timeout"
        bound.warning("job.timeout", timeout_s=job["timeout_s"])

    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        status = ExecutionStatus.FAILED
        error_message = f"{type(exc).__name__}: {exc}"
        error_stack = traceback.format_exc()
        bound.warning("job.failed", error=error_message)

    # ---- record -----------------------------------------------------------
    async with session_scope() as db:
        await _record_execution(
            db,
            job_id=job_id,
            attempt=attempt,
            worker_id=worker_id,
            status=status,
            started_at=started_at,
            error_message=error_message,
            error_stack=error_stack,
            output=output,
            log_lines=ctx.drain(),
        )

    # ---- transition the job ------------------------------------------------
    if status is ExecutionStatus.SUCCEEDED:
        async with session_scope() as db:
            ok = await complete_job(db, job_id, lock_token, json.dumps(output or {}))
        if not ok:
            bound.warning("job.stale_completion_discarded")
            return "lost"
        return "completed"

    # Failure: retry with backoff, or exhaust.
    if attempt >= max_attempts:
        # One statement marks the job dead and writes its dead-letter record.
        # The stack goes with it: an operator triaging the DLQ needs the
        # traceback of the *final* attempt, and the job row has nowhere to keep
        # one.
        async with session_scope() as db:
            await exhaust_job(
                db, job_id, lock_token, error_message or "failed", error_stack
            )
        bound.error("job.exhausted", attempts=attempt)
        return "dead"

    async with session_scope() as db:
        policy = (
            await db.execute(
                select(RetryPolicy)
                .join(Queue, Queue.retry_policy_id == RetryPolicy.id)
                .where(Queue.id == job["queue_id"])
            )
        ).scalar_one_or_none()

        delay = (
            compute_delay_seconds(
                policy.strategy,
                attempt,
                policy.base_delay_ms,
                policy.max_delay_ms,
                policy.jitter,
            )
            if policy
            else 5.0
        )
        await retry_job(db, job_id, lock_token, delay, error_message or "failed")

    bound.info("job.retry_scheduled", delay_s=round(delay, 2), next_attempt=attempt + 1)
    return "retried"
