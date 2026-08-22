"""Recurring schedule management.

The API owns creating, editing and deleting cron templates; the scheduler
process owns firing them. The only place the two meet is `next_run_at`, which
this module computes on write and the scheduler advances on fire -- using the
same `cron.next_fire_time`, so a schedule's first fire and its four hundredth
are computed by identical code.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.errors import ConflictError, NotFoundError, ValidationError
from apps.api.schemas.schedule import ScheduleCreate, ScheduleUpdate
from apps.scheduler.cron import CronError, next_fire_time, validate
from packages.db import Queue, RetryPolicy, ScheduledJob

log = structlog.get_logger(__name__)


async def create_schedule(
    db: AsyncSession, queue: Queue, payload: ScheduleCreate
) -> ScheduledJob:
    """Create a cron template and compute its first fire time.

    `start_at` lets an operator pin the first occurrence -- useful for staging a
    schedule that should not begin until a migration completes. Omitted, the
    first fire is simply the next occurrence from now, which is what "every
    weekday at 09:00" means to everyone who is not a scheduler.
    """
    existing = await db.scalar(
        select(ScheduledJob).where(
            ScheduledJob.queue_id == queue.id, ScheduledJob.name == payload.name
        )
    )
    if existing is not None:
        raise ConflictError(
            f"A schedule named '{payload.name}' already exists on this queue"
        )

    first_run = payload.start_at or next_fire_time(
        payload.cron_expression, payload.timezone
    )
    if first_run.tzinfo is None:
        first_run = first_run.replace(tzinfo=UTC)

    schedule = ScheduledJob(
        queue_id=queue.id,
        name=payload.name,
        cron_expression=payload.cron_expression,
        timezone=payload.timezone,
        job_type=payload.job_type,
        payload=payload.payload,
        priority=payload.priority,
        is_active=payload.is_active,
        next_run_at=first_run,
    )
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)

    log.info(
        "schedule.created",
        schedule_id=str(schedule.id),
        name=schedule.name,
        cron=schedule.cron_expression,
        timezone=schedule.timezone,
        next_run_at=schedule.next_run_at.isoformat(),
    )
    return schedule


async def get_schedule(
    db: AsyncSession, project_id: uuid.UUID, schedule_id: uuid.UUID
) -> ScheduledJob:
    """Fetch one schedule, scoped to the caller's project."""
    stmt = (
        select(ScheduledJob)
        .join(Queue, Queue.id == ScheduledJob.queue_id)
        .where(ScheduledJob.id == schedule_id, Queue.project_id == project_id)
    )
    schedule = (await db.execute(stmt)).scalar_one_or_none()
    if schedule is None:
        raise NotFoundError("Schedule not found")
    return schedule


async def list_schedules(
    db: AsyncSession,
    project_id: uuid.UUID,
    queue_id: uuid.UUID | None = None,
    active_only: bool = False,
) -> list[ScheduledJob]:
    """List schedules for a project.

    Not keyset-paginated, unlike jobs: schedules are configuration written by
    hand, so a project has tens of them, not millions. Paginating a list that
    fits on one screen would be ceremony without benefit.
    """
    stmt = (
        select(ScheduledJob)
        .join(Queue, Queue.id == ScheduledJob.queue_id)
        .where(Queue.project_id == project_id)
    )
    if queue_id is not None:
        stmt = stmt.where(ScheduledJob.queue_id == queue_id)
    if active_only:
        stmt = stmt.where(ScheduledJob.is_active.is_(True))

    stmt = stmt.order_by(ScheduledJob.next_run_at.asc())
    return list((await db.execute(stmt)).scalars().all())


async def update_schedule(
    db: AsyncSession,
    project_id: uuid.UUID,
    schedule_id: uuid.UUID,
    payload: ScheduleUpdate,
) -> ScheduledJob:
    """Edit a schedule, recomputing `next_run_at` when the timing changes.

    The expression and the timezone are validated *as a pair* against the merged
    result, not against whichever half the request happened to include. Changing
    only the timezone still has to produce a schedule the scheduler can fire.
    """
    schedule = await get_schedule(db, project_id, schedule_id)
    changes = payload.model_dump(exclude_unset=True)

    new_cron = changes.get("cron_expression", schedule.cron_expression)
    new_tz = changes.get("timezone", schedule.timezone)
    timing_changed = (
        new_cron != schedule.cron_expression or new_tz != schedule.timezone
    )

    if timing_changed:
        try:
            validate(new_cron, new_tz)
        except CronError as exc:
            raise ValidationError(str(exc)) from exc

    for field, value in changes.items():
        setattr(schedule, field, value)

    # Reactivating a schedule also recomputes: a template paused for a week has
    # a `next_run_at` deep in the past, and firing it immediately on resume
    # would be a surprise, not a feature.
    reactivated = changes.get("is_active") is True
    if timing_changed or reactivated:
        schedule.next_run_at = next_fire_time(new_cron, new_tz)

    await db.commit()
    await db.refresh(schedule)
    log.info("schedule.updated", schedule_id=str(schedule_id), fields=list(changes))
    return schedule


async def delete_schedule(
    db: AsyncSession, project_id: uuid.UUID, schedule_id: uuid.UUID
) -> None:
    """Delete a template. Jobs it already produced are untouched.

    `jobs.scheduled_job_id` is ON DELETE SET NULL for exactly this reason: the
    history of what a schedule ran must outlive the schedule, or deleting a
    misconfigured cron would erase the evidence of what it did.
    """
    schedule = await get_schedule(db, project_id, schedule_id)
    await db.delete(schedule)
    await db.commit()
    log.info("schedule.deleted", schedule_id=str(schedule_id))


#: Manual fire.
#:
#: Deliberately does NOT advance `next_run_at`: an operator testing a schedule
#: is asking "does this work", not "reschedule everything". The idempotency key
#: is keyed on the trigger instant rather than on an occurrence, so repeated
#: manual triggers each produce a job while the cron path stays deduplicated.
TRIGGER_SQL = text("""
INSERT INTO jobs (
    queue_id, job_type, payload, status, priority,
    max_attempts, timeout_s, run_at, idempotency_key, scheduled_job_id
)
VALUES (
    :queue_id, :job_type, CAST(:payload AS jsonb), 'queued', :priority,
    :max_attempts, :timeout_s, now(), :idempotency_key, :scheduled_job_id
)
RETURNING id
""")


async def trigger_schedule(
    db: AsyncSession, project_id: uuid.UUID, schedule_id: uuid.UUID
) -> tuple[uuid.UUID, ScheduledJob]:
    """Fire a schedule now, out of band. Returns the created job id."""
    schedule = await get_schedule(db, project_id, schedule_id)

    queue = await db.get(Queue, schedule.queue_id)
    if queue is None:
        raise NotFoundError("Schedule's queue no longer exists")
    policy = await db.get(RetryPolicy, queue.retry_policy_id)
    if policy is None:
        raise NotFoundError("Queue's retry policy is missing")

    now = datetime.now(UTC)
    job_id = (
        await db.execute(
            TRIGGER_SQL,
            {
                "queue_id": schedule.queue_id,
                "job_type": schedule.job_type,
                "payload": json.dumps(schedule.payload or {}),
                "priority": schedule.priority,
                "max_attempts": policy.max_attempts,
                "timeout_s": queue.default_timeout_s,
                "idempotency_key": f"manual:{schedule.id}:{now.isoformat()}",
                "scheduled_job_id": schedule.id,
            },
        )
    ).scalar_one()

    await db.commit()
    log.info(
        "schedule.manually_triggered",
        schedule_id=str(schedule_id),
        job_id=str(job_id),
    )
    return job_id, schedule
