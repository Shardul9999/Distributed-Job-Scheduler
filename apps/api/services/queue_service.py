"""Queue and retry-policy operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.errors import ConflictError, NotFoundError, ValidationError
from apps.api.schemas.queue import (
    QueueCreate,
    QueueUpdate,
    RetryPolicyCreate,
    RetryPolicyUpdate,
)
from packages.db import ExecutionStatus, Job, JobExecution, Queue, RetryPolicy
from packages.db.enums import JobStatus, RetryStrategy

log = structlog.get_logger(__name__)


# =============================================================================
# Retry policies
# =============================================================================


async def create_policy(
    db: AsyncSession, project_id: uuid.UUID, payload: RetryPolicyCreate
) -> RetryPolicy:
    policy = RetryPolicy(project_id=project_id, **payload.model_dump())
    db.add(policy)
    await db.commit()
    await db.refresh(policy)
    log.info("retry_policy.created", policy_id=str(policy.id))
    return policy


async def list_policies(
    db: AsyncSession, project_id: uuid.UUID
) -> list[RetryPolicy]:
    stmt = (
        select(RetryPolicy)
        .where(RetryPolicy.project_id == project_id)
        .order_by(RetryPolicy.name)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_or_create_default_policy(
    db: AsyncSession, project_id: uuid.UUID
) -> RetryPolicy:
    """Every queue needs a policy (the FK is NOT NULL), so creating a queue
    without naming one materializes a sensible default rather than failing."""
    existing = await db.scalar(
        select(RetryPolicy).where(
            RetryPolicy.project_id == project_id, RetryPolicy.name == "default"
        )
    )
    if existing is not None:
        return existing

    policy = RetryPolicy(
        project_id=project_id,
        name="default",
        strategy=RetryStrategy.EXPONENTIAL,
        max_attempts=3,
        base_delay_ms=1000,
        max_delay_ms=3_600_000,
        jitter=True,
    )
    db.add(policy)
    await db.flush()
    return policy


async def update_policy(
    db: AsyncSession, policy_id: uuid.UUID, payload: RetryPolicyUpdate
) -> RetryPolicy:
    policy = await db.get(RetryPolicy, policy_id)
    if policy is None:
        raise NotFoundError("Retry policy not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(policy, field, value)

    if policy.max_delay_ms < policy.base_delay_ms:
        raise ValidationError("max_delay_ms must be >= base_delay_ms")

    await db.commit()
    await db.refresh(policy)
    return policy


async def delete_policy(db: AsyncSession, policy_id: uuid.UUID) -> None:
    """Delete a policy. The FK is ON DELETE RESTRICT, so this raises a 409 via
    the IntegrityError handler if any queue still references it -- which is the
    intended behaviour, not a bug."""
    policy = await db.get(RetryPolicy, policy_id)
    if policy is None:
        raise NotFoundError("Retry policy not found")
    await db.delete(policy)
    await db.commit()


# =============================================================================
# Queues
# =============================================================================


async def create_queue(
    db: AsyncSession, project_id: uuid.UUID, payload: QueueCreate
) -> Queue:
    existing = await db.scalar(
        select(Queue).where(Queue.project_id == project_id, Queue.name == payload.name)
    )
    if existing is not None:
        raise ConflictError(f"A queue named '{payload.name}' already exists")

    data = payload.model_dump(exclude={"retry_policy_id"})

    if payload.retry_policy_id is not None:
        policy = await db.get(RetryPolicy, payload.retry_policy_id)
        # Verify the policy belongs to this project. Without this check a
        # caller could bind their queue to another tenant's policy by id.
        if policy is None or policy.project_id != project_id:
            raise NotFoundError("Retry policy not found in this project")
        policy_id = policy.id
    else:
        policy_id = (await get_or_create_default_policy(db, project_id)).id

    queue = Queue(project_id=project_id, retry_policy_id=policy_id, **data)
    db.add(queue)
    await db.commit()
    await db.refresh(queue)

    log.info("queue.created", queue_id=str(queue.id), name=queue.name)
    return queue


async def list_queues(db: AsyncSession, project_id: uuid.UUID) -> list[Queue]:
    stmt = (
        select(Queue)
        .where(Queue.project_id == project_id)
        .order_by(Queue.priority.desc(), Queue.name)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_queue(db: AsyncSession, queue_id: uuid.UUID) -> Queue:
    queue = await db.get(Queue, queue_id)
    if queue is None:
        raise NotFoundError("Queue not found")
    return queue


async def update_queue(
    db: AsyncSession, queue_id: uuid.UUID, payload: QueueUpdate
) -> Queue:
    queue = await get_queue(db, queue_id)

    data = payload.model_dump(exclude_unset=True)
    if "retry_policy_id" in data and data["retry_policy_id"] is not None:
        policy = await db.get(RetryPolicy, data["retry_policy_id"])
        if policy is None or policy.project_id != queue.project_id:
            raise NotFoundError("Retry policy not found in this project")

    for field, value in data.items():
        setattr(queue, field, value)

    await db.commit()
    await db.refresh(queue)
    log.info("queue.updated", queue_id=str(queue_id))
    return queue


async def set_paused(db: AsyncSession, queue_id: uuid.UUID, paused: bool) -> Queue:
    """Pause or resume a queue.

    Pausing does not touch a single job row. The `headroom` CTE in the claim
    query yields no rows for a paused queue, so workers simply stop finding
    work there -- and jobs already running are allowed to finish rather than
    being killed. Resuming is instant for the same reason.
    """
    queue = await get_queue(db, queue_id)
    queue.is_paused = paused
    await db.commit()
    await db.refresh(queue)
    log.info("queue.paused" if paused else "queue.resumed", queue_id=str(queue_id))
    return queue


async def delete_queue(db: AsyncSession, queue_id: uuid.UUID) -> None:
    queue = await get_queue(db, queue_id)
    await db.delete(queue)
    await db.commit()
    log.warning("queue.deleted", queue_id=str(queue_id))


async def get_stats(db: AsyncSession, queue_id: uuid.UUID) -> dict:
    """Depth and health for one queue.

    Status counts come from a single grouped scan rather than eight separate
    COUNT queries, and the rolling-window metrics come from one more over
    job_executions. Two queries total for the whole panel.
    """
    queue = await get_queue(db, queue_id)

    counts_stmt = (
        select(Job.status, func.count().label("n"))
        .where(Job.queue_id == queue_id)
        .group_by(Job.status)
    )
    counts = {row.status: row.n for row in (await db.execute(counts_stmt)).all()}

    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    window_stmt = select(
        func.count()
        .filter(JobExecution.status == ExecutionStatus.SUCCEEDED)
        .label("completed"),
        func.count()
        .filter(JobExecution.status != ExecutionStatus.SUCCEEDED)
        .label("failed"),
        func.avg(JobExecution.duration_ms).label("avg_ms"),
    ).select_from(JobExecution).join(Job, Job.id == JobExecution.job_id).where(
        Job.queue_id == queue_id, JobExecution.started_at >= hour_ago
    )
    window = (await db.execute(window_stmt)).one()

    # Age of the oldest waiting job -- the single most useful signal that a
    # queue is stuck: depth can look healthy while the head of the queue is
    # hours old.
    oldest_stmt = select(func.min(Job.run_at)).where(
        Job.queue_id == queue_id,
        Job.status.in_([JobStatus.QUEUED, JobStatus.SCHEDULED]),
        Job.run_at <= func.now(),
    )
    oldest = (await db.execute(oldest_stmt)).scalar_one_or_none()

    def n(status: JobStatus) -> int:
        return counts.get(status, 0)

    return {
        "queue_id": queue.id,
        "name": queue.name,
        "is_paused": queue.is_paused,
        "queued": n(JobStatus.QUEUED),
        "scheduled": n(JobStatus.SCHEDULED),
        "claimed": n(JobStatus.CLAIMED),
        "running": n(JobStatus.RUNNING),
        "completed": n(JobStatus.COMPLETED),
        "failed": n(JobStatus.FAILED),
        "dead": n(JobStatus.DEAD),
        "cancelled": n(JobStatus.CANCELLED),
        "backlog": n(JobStatus.QUEUED) + n(JobStatus.SCHEDULED),
        "in_flight": n(JobStatus.CLAIMED) + n(JobStatus.RUNNING),
        "completed_last_hour": window.completed or 0,
        "failed_last_hour": window.failed or 0,
        "avg_duration_ms": float(window.avg_ms) if window.avg_ms is not None else None,
        "oldest_queued_age_s": (
            (datetime.now(UTC) - oldest).total_seconds() if oldest else None
        ),
    }
