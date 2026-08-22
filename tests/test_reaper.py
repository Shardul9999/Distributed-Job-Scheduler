"""Crash recovery, and the fencing that makes it safe.

The reaper's job is easy to state and easy to get subtly wrong: bring back a job
whose worker died. The subtlety is that "died" is unknowable. All we ever
observe is silence, and a silent worker may be dead, or may be paused, swapping,
or on the far side of a network partition and about to come back with a result
for an attempt we have already given to someone else.

These tests assert both halves: that the job comes back, and that the worker
which came back cannot corrupt it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.scheduler import reaper
from packages.db import DeadLetterEntry, Job, JobExecution, Worker
from packages.db.claim import claim_jobs, complete_job
from packages.db.enums import ExecutionStatus, JobStatus, WorkerStatus


@pytest_asyncio.fixture(autouse=True)
async def _isolate_fleet(db: AsyncSession) -> None:
    """Give each reaper test an empty fleet.

    `recover_orphans` and `mark_dead_workers` sweep the entire database on
    purpose -- a reaper that only looked at one project would leave every other
    project's crashed jobs stranded. The rest of the suite shares one migrated
    database and isolates by unique tags, but a fleet-wide sweep cannot be
    tag-isolated: one test's orphaned jobs would be counted by another test's
    assertion on the global `(requeued, dead_lettered)` tuple (the flake that
    made the idempotency test see 200 orphans instead of 1).

    Truncating just the job/worker tables before each test restores isolation
    without a global teardown. Scaffold's org/project/queue rows are untouched,
    and this only ever runs against TEST_DATABASE_URL.
    """
    await db.execute(
        text(
            "TRUNCATE job_executions, dead_letter_queue, jobs, "
            "worker_heartbeats, workers RESTART IDENTITY CASCADE"
        )
    )
    await db.commit()


async def _worker(db: AsyncSession, *, status=WorkerStatus.ACTIVE, beat_age_s=0):
    result = await db.execute(
        insert(Worker)
        .values(
            hostname="reaper-test",
            pid=4242,
            version="test",
            concurrency=5,
            status=status,
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=beat_age_s),
        )
        .returning(Worker.id)
    )
    await db.commit()
    return result.scalar_one()


async def _job(db: AsyncSession, queue_id: uuid.UUID, max_attempts: int = 3):
    result = await db.execute(
        insert(Job)
        .values(
            queue_id=queue_id,
            job_type="echo",
            payload={},
            status=JobStatus.QUEUED,
            max_attempts=max_attempts,
            timeout_s=30,
        )
        .returning(Job.id)
    )
    await db.commit()
    return result.scalar_one()


async def test_silent_worker_is_declared_dead(db: AsyncSession) -> None:
    """No heartbeat for longer than the threshold means dead."""
    healthy = await _worker(db, beat_age_s=5)
    silent = await _worker(db, beat_age_s=300)

    marked = await reaper.mark_dead_workers(db, threshold_s=60)
    await db.commit()

    assert marked >= 1
    assert (await db.get(Worker, silent)).status is WorkerStatus.DEAD
    # And a worker that is merely quiet-ish is left alone. A reaper that evicts
    # healthy workers is worse than no reaper: it manufactures the very
    # duplicate-execution risk it exists to contain.
    await db.refresh(await db.get(Worker, healthy))
    assert (await db.get(Worker, healthy)).status is WorkerStatus.ACTIVE


async def test_job_held_by_dead_worker_is_requeued(
    db: AsyncSession, scaffold: dict
) -> None:
    """The headline recovery path, and the `kill -9` gate in miniature."""
    queue = scaffold["queue"]
    worker_id = await _worker(db)
    await _job(db, queue.id)

    claimed = await claim_jobs(db, queue.id, worker_id, batch_size=1)
    assert len(claimed) == 1
    job_id = claimed[0]["id"]

    # The worker vanishes: no drain, no release, nothing. Exactly what SIGKILL
    # looks like from the database's point of view.
    await db.execute(
        update(Worker).where(Worker.id == worker_id).values(status=WorkerStatus.DEAD)
    )
    await db.commit()

    requeued, dead_lettered = await reaper.recover_orphans(db)
    assert (requeued, dead_lettered) == (1, 0)

    job = await db.get(Job, job_id, populate_existing=True)
    assert job.status is JobStatus.QUEUED, "job was not returned to the queue"
    assert job.claimed_by is None
    assert job.lock_token is None

    # The attempt is NOT refunded. It really was attempted, and refunding it
    # would let a job cycle forever between crashing workers without ever
    # reaching the DLQ.
    assert job.attempt == 1

    # The failure is visible in history as infrastructure, not as application
    # error -- the distinction ExecutionStatus.LOST exists to preserve.
    execution = (
        await db.execute(select(JobExecution).where(JobExecution.job_id == job_id))
    ).scalar_one()
    assert execution.status is ExecutionStatus.LOST
    assert execution.attempt_number == 1
    assert execution.worker_id == worker_id


async def test_revived_job_fences_out_the_zombie(
    db: AsyncSession, scaffold: dict
) -> None:
    """A worker that comes back from the dead cannot overwrite the live attempt.

    This is the test that matters most. Recovery without fencing is not
    recovery -- it is a second way to execute a job twice, arrived at by a
    longer route.
    """
    queue = scaffold["queue"]
    worker_id = await _worker(db)
    await _job(db, queue.id)

    claimed = await claim_jobs(db, queue.id, worker_id, batch_size=1)
    job_id, stale_token = claimed[0]["id"], claimed[0]["lock_token"]

    await db.execute(
        update(Worker).where(Worker.id == worker_id).values(status=WorkerStatus.DEAD)
    )
    await db.commit()
    await reaper.recover_orphans(db)

    # The partition heals. The zombie finished its work and reports success,
    # holding the token it was issued before it went silent.
    accepted = await complete_job(db, job_id, stale_token, '{"stale": true}')
    assert accepted is False, "a stale worker's result was accepted"

    job = await db.get(Job, job_id, populate_existing=True)
    assert job.status is JobStatus.QUEUED, (
        "the zombie's write reached the job row and overwrote the recovery"
    )
    assert job.result is None


async def test_expired_visibility_timeout_recovers_without_a_worker_row(
    db: AsyncSession, scaffold: dict
) -> None:
    """The safety net: a claim older than the visibility timeout is recovered
    even when the worker still looks alive.

    This covers the worker that is running but wedged -- a handler stuck on a
    socket with no timeout, a process swapping itself to death. Its heartbeat
    loop may still be ticking while the job goes nowhere.
    """
    queue = scaffold["queue"]
    queue.visibility_timeout_s = 1
    await db.commit()

    worker_id = await _worker(db, beat_age_s=0)
    await _job(db, queue.id)

    claimed = await claim_jobs(db, queue.id, worker_id, batch_size=1)
    job_id = claimed[0]["id"]

    # Age the claim past the queue's promise rather than sleeping through it.
    await db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(claimed_at=datetime.now(UTC) - timedelta(seconds=30))
    )
    await db.commit()

    requeued, _ = await reaper.recover_orphans(db)
    assert requeued == 1
    assert (await db.get(Job, job_id, populate_existing=True)).status is JobStatus.QUEUED


async def test_repeatedly_lost_job_eventually_dead_letters(
    db: AsyncSession, scaffold: dict
) -> None:
    """A job cannot be lost forever.

    Losing an attempt consumes a retry, so a job whose workers keep dying
    reaches the same terminal state as a job whose handler keeps raising -- and
    lands in the DLQ with a reason that names infrastructure rather than blaming
    the payload.
    """
    queue = scaffold["queue"]
    job_id = await _job(db, queue.id, max_attempts=1)
    worker_id = await _worker(db)

    await claim_jobs(db, queue.id, worker_id, batch_size=1)
    await db.execute(
        update(Worker).where(Worker.id == worker_id).values(status=WorkerStatus.DEAD)
    )
    await db.commit()

    requeued, dead_lettered = await reaper.recover_orphans(db)
    assert (requeued, dead_lettered) == (0, 1)

    job = await db.get(Job, job_id, populate_existing=True)
    assert job.status is JobStatus.DEAD

    entry = (
        await db.execute(
            select(DeadLetterEntry).where(DeadLetterEntry.job_id == job_id)
        )
    ).scalar_one()
    assert entry.total_attempts == 1
    assert "stopped reporting" in entry.failure_reason


async def test_reaper_is_idempotent(db: AsyncSession, scaffold: dict) -> None:
    """A second sweep over the same job does nothing.

    Two scheduler replicas both sweeping, or one sweeping twice after a retry,
    must not produce two `lost` rows or double-charge the attempt counter.
    """
    queue = scaffold["queue"]
    worker_id = await _worker(db)
    await _job(db, queue.id)
    claimed = await claim_jobs(db, queue.id, worker_id, batch_size=1)
    job_id = claimed[0]["id"]

    await db.execute(
        update(Worker).where(Worker.id == worker_id).values(status=WorkerStatus.DEAD)
    )
    await db.commit()

    assert await reaper.recover_orphans(db) == (1, 0)
    assert await reaper.recover_orphans(db) == (0, 0)

    executions = await db.scalar(
        select(func.count()).select_from(JobExecution).where(JobExecution.job_id == job_id)
    )
    assert executions == 1


async def test_heartbeat_retention_trims_old_samples(db: AsyncSession) -> None:
    """The time series is bounded."""
    worker_id = await _worker(db)
    await db.execute(
        text(
            "INSERT INTO worker_heartbeats (worker_id, beat_at, active_jobs, "
            "jobs_processed) SELECT :w, now() - make_interval(hours => g), 0, 0 "
            "FROM generate_series(0, 48) g"
        ),
        {"w": worker_id},
    )
    await db.commit()

    trimmed = await reaper.trim_heartbeats(db, retention_hours=24)
    assert trimmed > 0

    remaining = await db.scalar(
        text(
            "SELECT count(*) FROM worker_heartbeats WHERE worker_id = :w "
            "AND beat_at < now() - interval '24 hours'"
        ),
        {"w": worker_id},
    )
    assert remaining == 0
