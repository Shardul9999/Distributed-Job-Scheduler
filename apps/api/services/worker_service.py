"""Worker fleet observability.

Read-only. Nothing here mutates a worker: workers are self-registering
processes, and the only lifecycle signal the platform sends them is SIGTERM
from the orchestrator. An API that pretended to stop a worker would be lying
about a process it has no channel to.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.errors import NotFoundError
from apps.api.schemas.worker import FleetStatsResponse
from packages.db import (
    DeadLetterEntry,
    Job,
    ScheduledJob,
    Worker,
    WorkerHeartbeat,
)
from packages.db.enums import JobStatus, WorkerStatus
from packages.locks import SCHEDULER_LOCK_KEY

#: Is anyone currently leading?
#:
#: `pg_locks` exposes advisory locks directly, so "is the scheduler alive" is
#: answerable without the scheduler reporting anything -- no heartbeat table, no
#: staleness threshold, no risk of a scheduler that has crashed still looking
#: healthy because its last self-report has not expired yet. The lock is held on
#: the leader's session; if that session is gone, so is the row.
#:
#: The single-argument form of pg_advisory_lock stores the key as
#: (classid = key >> 32, objid = key & 0xffffffff, objsubid = 1).
LEADER_PRESENT_SQL = text("""
SELECT EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory'
      AND classid  = 0
      AND objid    = :key
      AND objsubid = 1
      AND granted
)
""")


async def list_workers(
    db: AsyncSession, include_stopped: bool = False
) -> list[Worker]:
    """The fleet, liveliest first.

    Dead and stopped rows are hidden by default: they are history, and an
    operator opening the Workers page wants to know what is running now. The
    flag exists because "which worker was holding this job when it died" is a
    real question during an incident.
    """
    stmt = select(Worker)
    if not include_stopped:
        stmt = stmt.where(
            Worker.status.in_([WorkerStatus.STARTING, WorkerStatus.ACTIVE, WorkerStatus.DRAINING])
        )
    stmt = stmt.order_by(Worker.last_heartbeat_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def get_worker(db: AsyncSession, worker_id: uuid.UUID) -> Worker:
    worker = await db.get(Worker, worker_id)
    if worker is None:
        raise NotFoundError("Worker not found")
    return worker


async def recent_heartbeats(
    db: AsyncSession, worker_id: uuid.UUID, limit: int = 60
) -> list[WorkerHeartbeat]:
    """The last N samples, for the utilisation sparkline on the worker detail
    page. Sixty beats at the default ten-second interval is the last ten
    minutes -- enough to see a worker saturating, short enough to stay cheap."""
    stmt = (
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_id == worker_id)
        .order_by(WorkerHeartbeat.beat_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def fleet_stats(db: AsyncSession) -> FleetStatsResponse:
    """One overview payload for the dashboard's landing page.

    Three grouped scans plus two counts rather than a dozen single-value
    queries: every number below is derived from an aggregate the database can
    answer from an index, and the whole endpoint is one round trip per group.
    """
    stats = FleetStatsResponse()

    worker_rows = (
        await db.execute(
            select(
                Worker.status,
                func.count().label("n"),
                func.coalesce(func.sum(Worker.concurrency), 0).label("capacity"),
            ).group_by(Worker.status)
        )
    ).all()

    for row in worker_rows:
        stats.workers_total += row.n
        match row.status:
            case WorkerStatus.ACTIVE:
                stats.workers_active = row.n
                stats.fleet_capacity += row.capacity
            case WorkerStatus.DRAINING:
                stats.workers_draining = row.n
                # Draining workers still finish what they hold, so their slots
                # are part of current capacity even though they take no new work.
                stats.fleet_capacity += row.capacity
            case WorkerStatus.DEAD:
                stats.workers_dead = row.n
            case WorkerStatus.STOPPED:
                stats.workers_stopped = row.n
            case _:
                pass

    job_rows = (
        await db.execute(
            select(Job.status, func.count().label("n")).group_by(Job.status)
        )
    ).all()

    by_status = {row.status: row.n for row in job_rows}
    stats.jobs_in_flight = by_status.get(JobStatus.CLAIMED, 0) + by_status.get(
        JobStatus.RUNNING, 0
    )
    stats.jobs_backlog = by_status.get(JobStatus.QUEUED, 0) + by_status.get(
        JobStatus.SCHEDULED, 0
    )
    stats.jobs_dead = by_status.get(JobStatus.DEAD, 0)

    stats.dlq_unreplayed = (
        await db.scalar(
            select(func.count())
            .select_from(DeadLetterEntry)
            .where(DeadLetterEntry.replayed_at.is_(None))
        )
    ) or 0

    stats.schedules_active = (
        await db.scalar(
            select(func.count())
            .select_from(ScheduledJob)
            .where(ScheduledJob.is_active.is_(True))
        )
    ) or 0

    stats.scheduler_leader_present = bool(
        await db.scalar(LEADER_PRESENT_SQL, {"key": SCHEDULER_LOCK_KEY})
    )

    return stats
