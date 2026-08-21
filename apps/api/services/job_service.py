"""Job enqueue, query, and manual lifecycle operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.errors import ConflictError, NotFoundError, ValidationError
from apps.api.core.pagination import PageParams, build_page
from apps.api.schemas.job import BatchJobCreate, JobCreate, JobFilters
from packages.db import Job, JobExecution, JobLog, Queue, RetryPolicy
from packages.db.enums import CLAIMABLE_STATUSES, TERMINAL_STATUSES, JobStatus

log = structlog.get_logger(__name__)


async def _resolve_defaults(
    db: AsyncSession, queue: Queue
) -> tuple[RetryPolicy, int, int]:
    policy = await db.get(RetryPolicy, queue.retry_policy_id)
    if policy is None:
        raise NotFoundError("Queue's retry policy is missing")
    return policy, policy.max_attempts, queue.default_timeout_s


def _build_job(
    queue: Queue,
    payload: JobCreate,
    policy_max_attempts: int,
    queue_timeout: int,
    batch_id: uuid.UUID | None = None,
) -> Job:
    run_at = payload.effective_run_at

    # SCHEDULED vs QUEUED is purely "is it due yet". The claim query accepts
    # either as long as run_at has passed, so this classification is for the
    # dashboard's benefit rather than the worker's.
    status = (
        JobStatus.SCHEDULED
        if run_at > datetime.now(UTC)
        else JobStatus.QUEUED
    )

    return Job(
        queue_id=queue.id,
        job_type=payload.job_type,
        payload=payload.payload,
        status=status,
        # Job-level overrides fall back to the queue's or the policy's value.
        # Snapshotted at enqueue time so that later edits to the queue or policy
        # cannot retroactively change the contract of a job already in flight.
        priority=payload.priority if payload.priority is not None else queue.priority,
        max_attempts=payload.max_attempts or policy_max_attempts,
        timeout_s=payload.timeout_s or queue_timeout,
        run_at=run_at,
        idempotency_key=payload.idempotency_key,
        batch_id=batch_id,
    )


async def enqueue(db: AsyncSession, queue: Queue, payload: JobCreate) -> Job:
    """Create a single job -- immediate, delayed, or scheduled.

    Idempotency is enforced by the partial unique index
    `idx_jobs_idempotency (queue_id, idempotency_key)`. We check first for a
    friendly response, but the index is the actual guarantee: two concurrent
    requests with the same key cannot both insert, and the loser surfaces as a
    409 through the IntegrityError handler.
    """
    if payload.idempotency_key:
        existing = await db.scalar(
            select(Job).where(
                Job.queue_id == queue.id,
                Job.idempotency_key == payload.idempotency_key,
            )
        )
        if existing is not None:
            log.info("job.idempotent_hit", job_id=str(existing.id))
            return existing

    _, max_attempts, timeout = await _resolve_defaults(db, queue)
    job = _build_job(queue, payload, max_attempts, timeout)

    db.add(job)
    await db.commit()
    await db.refresh(job)

    log.info(
        "job.enqueued",
        job_id=str(job.id),
        queue=queue.name,
        job_type=job.job_type,
        status=job.status.value,
    )
    return job


async def enqueue_batch(
    db: AsyncSession, queue: Queue, payload: BatchJobCreate
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Create many jobs in one transaction, sharing a batch_id.

    All-or-nothing: a batch that half-inserts is worse than one that fails,
    because the caller cannot tell which half landed.
    """
    _, max_attempts, timeout = await _resolve_defaults(db, queue)
    batch_id = uuid.uuid4()

    keys = [j.idempotency_key for j in payload.jobs if j.idempotency_key]
    if len(keys) != len(set(keys)):
        raise ValidationError("Duplicate idempotency_key values within the batch")

    jobs = [
        _build_job(queue, spec, max_attempts, timeout, batch_id=batch_id)
        for spec in payload.jobs
    ]
    db.add_all(jobs)
    await db.commit()

    log.info("job.batch_enqueued", batch_id=str(batch_id), count=len(jobs))
    return batch_id, [j.id for j in jobs]


async def list_jobs(
    db: AsyncSession,
    project_id: uuid.UUID,
    filters: JobFilters,
    params: PageParams,
) -> tuple[list[Job], str | None, bool]:
    """Paginated, filtered job listing for the Job Explorer.

    Keyset pagination on (created_at, id). The composite tuple comparison is
    what makes the cursor total-ordered: `created_at` alone is not unique, so
    paging on it would skip or repeat rows whenever two jobs share a timestamp
    -- which is routine for a batch insert.
    """
    stmt = (
        select(Job)
        .join(Queue, Queue.id == Job.queue_id)
        .where(Queue.project_id == project_id)
    )

    if filters.queue_id is not None:
        stmt = stmt.where(Job.queue_id == filters.queue_id)
    if filters.status is not None:
        stmt = stmt.where(Job.status == filters.status)
    if filters.job_type is not None:
        stmt = stmt.where(Job.job_type == filters.job_type)
    if filters.batch_id is not None:
        stmt = stmt.where(Job.batch_id == filters.batch_id)

    cursor = params.decoded_cursor
    if cursor is not None:
        stmt = stmt.where(tuple_(Job.created_at, Job.id) < (cursor.ts, cursor.id))

    # Over-fetch by one to detect a next page without a second COUNT query.
    stmt = stmt.order_by(Job.created_at.desc(), Job.id.desc()).limit(params.limit + 1)

    rows = list((await db.execute(stmt)).scalars().all())
    return build_page(rows, params)


async def get_job(db: AsyncSession, project_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    """Fetch one job, scoped to the caller's project."""
    stmt = (
        select(Job)
        .join(Queue, Queue.id == Job.queue_id)
        .where(Job.id == job_id, Queue.project_id == project_id)
    )
    job = (await db.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise NotFoundError("Job not found")
    return job


async def list_executions(db: AsyncSession, job_id: uuid.UUID) -> list[JobExecution]:
    """Full attempt history, newest first."""
    stmt = (
        select(JobExecution)
        .where(JobExecution.job_id == job_id)
        .order_by(JobExecution.attempt_number.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_logs(
    db: AsyncSession, job_id: uuid.UUID, limit: int = 500
) -> list[JobLog]:
    stmt = (
        select(JobLog)
        .where(JobLog.job_id == job_id)
        .order_by(JobLog.logged_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def retry_job(db: AsyncSession, project_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    """Manually requeue a failed or dead job.

    `attempt` is reset to zero: an operator clicking Retry is making a fresh
    decision, and carrying the old attempt count would let the job die again
    immediately against an exhausted budget.
    """
    job = await get_job(db, project_id, job_id)

    if job.status not in (JobStatus.FAILED, JobStatus.DEAD, JobStatus.CANCELLED):
        raise ValidationError(
            f"Only failed, dead or cancelled jobs can be retried; this job is "
            f"{job.status.value}"
        )

    job.status = JobStatus.QUEUED
    job.attempt = 0
    job.run_at = datetime.now(UTC)
    job.claimed_by = None
    job.claimed_at = None
    job.started_at = None
    job.completed_at = None
    job.lock_token = None

    await db.commit()
    await db.refresh(job)
    log.info("job.manually_retried", job_id=str(job_id))
    return job


async def cancel_job(db: AsyncSession, project_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    """Cancel a job that has not started.

    Only claimable states can be cancelled. A RUNNING job is executing inside a
    worker process this API cannot reach, so reporting it as cancelled would be
    a lie -- the honest answer is to refuse and let the timeout handle it.
    """
    job = await get_job(db, project_id, job_id)

    if job.status in TERMINAL_STATUSES:
        raise ConflictError(f"Job is already {job.status.value}")
    if job.status not in CLAIMABLE_STATUSES:
        raise ConflictError(
            f"Job is already {job.status.value} on a worker and cannot be cancelled"
        )

    job.status = JobStatus.CANCELLED
    job.completed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(job)
    log.info("job.cancelled", job_id=str(job_id))
    return job
