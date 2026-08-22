"""Crash recovery: detect dead workers, revive the jobs they were holding.

This is the answer to the failure the assignment cares most about: a worker
process disappears -- `kill -9`, an OOM kill, a severed network -- while it
holds claimed jobs. Nothing in the claim path can help here, because the dead
process is by definition unable to hand its work back. Recovery has to be
driven from outside, by a component that notices the silence.

Three sweeps, in order:

    1. Liveness    workers whose heartbeat has lapsed are marked `dead`.
    2. Recovery    jobs still held by a dead worker, or whose claim outlived
                   its queue's visibility timeout, are requeued or dead-lettered.
    3. Retention   the heartbeat time series is trimmed.

**Why this is safe against a worker that is merely slow, not dead.** A network
partition can make a perfectly healthy worker look dead: it stops heartbeating,
we revive its job, another worker runs it -- and then the partition heals and
the original worker tries to report success for an attempt that no longer
belongs to it. That is the classic double-execution bug, and the fix is the
fencing token. Recovery sets `lock_token = NULL`, so every write the zombie
attempts (`WHERE lock_token = :token`) matches zero rows and is discarded. It
loses the race by construction rather than by timing.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.db.enums import RetryStrategy
from packages.retry import compute_delay_seconds

log = structlog.get_logger("reaper")


# ---------------------------------------------------------------------------
# Sweep 1 -- liveness
# ---------------------------------------------------------------------------
#
# Partial index `idx_workers_alive (last_heartbeat_at) WHERE status IN
# ('active','draining')` serves this exactly: in a healthy fleet the index holds
# a handful of rows and the sweep is effectively free.
#
# DRAINING workers are included deliberately. A worker that received SIGTERM and
# then hung mid-drain is just as dead as one that was killed outright, and its
# jobs are just as stranded.
#
MARK_DEAD_WORKERS_SQL = text("""
UPDATE workers
SET status     = 'dead',
    stopped_at = COALESCE(stopped_at, now())
WHERE status IN ('active', 'draining')
  AND last_heartbeat_at < now() - make_interval(secs => :threshold_s)
RETURNING id, hostname, pid, last_heartbeat_at
""")


async def mark_dead_workers(db: AsyncSession, threshold_s: int) -> int:
    """Flag workers that have stopped proving they are alive."""
    rows = (await db.execute(MARK_DEAD_WORKERS_SQL, {"threshold_s": threshold_s})).all()
    for row in rows:
        log.warning(
            "reaper.worker_declared_dead",
            worker_id=str(row.id),
            hostname=row.hostname,
            pid=row.pid,
            last_heartbeat_at=row.last_heartbeat_at.isoformat(),
            threshold_s=threshold_s,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Sweep 2 -- orphan recovery
# ---------------------------------------------------------------------------
#
# Two independent triggers, because they cover different failures:
#
#   worker is dead/stopped   Fast path. We already know nobody is running this,
#                            so there is no reason to wait out the timeout.
#   claim outlived the       Slow path, and the safety net. It catches a worker
#   visibility timeout       that vanished before its row was ever updated, a
#                            job whose worker row was purged, and any bug that
#                            leaves a claim dangling.
#
# `FOR UPDATE OF j SKIP LOCKED` for the same reason the claim query uses it: two
# scheduler replicas (or a scheduler and a manual recovery script) must never
# both recover the same job. Only `j` is locked -- the joined configuration rows
# are read-only here, and locking them would serialise unrelated recoveries.
#
FIND_ORPHANS_SQL = text("""
SELECT j.id,
       j.queue_id,
       j.attempt,
       j.max_attempts,
       j.claimed_by,
       j.claimed_at,
       j.started_at,
       q.visibility_timeout_s,
       rp.strategy      AS strategy,
       rp.base_delay_ms AS base_delay_ms,
       rp.max_delay_ms  AS max_delay_ms,
       rp.jitter        AS jitter,
       w.status         AS worker_status
FROM jobs j
JOIN queues q          ON q.id  = j.queue_id
JOIN retry_policies rp ON rp.id = q.retry_policy_id
LEFT JOIN workers w    ON w.id  = j.claimed_by
WHERE j.status IN ('claimed', 'running')
  AND (
        w.status IN ('dead', 'stopped')
     OR j.claimed_by IS NULL
     OR j.claimed_at < now() - make_interval(secs => q.visibility_timeout_s)
  )
ORDER BY j.claimed_at
LIMIT :batch_size
FOR UPDATE OF j SKIP LOCKED
""")

#: The attempt is recorded as `lost`, never as `failed`.
#:
#: That distinction is the whole reason ExecutionStatus.LOST exists. A `failed`
#: attempt means the handler raised and the payload is suspect; a `lost` attempt
#: means the infrastructure died and the payload was probably fine. Collapsing
#: them would send an operator hunting for a bug in code that never ran.
RECORD_LOST_SQL = text("""
INSERT INTO job_executions (
    job_id, attempt_number, worker_id, status,
    started_at, finished_at, duration_ms, error_message
)
SELECT :job_id,
       :attempt,
       :worker_id,
       'lost',
       s.began,
       now(),
       GREATEST(EXTRACT(EPOCH FROM (now() - s.began)) * 1000, 0)::int,
       :error
FROM (SELECT COALESCE(CAST(:started_at AS timestamptz), now()) AS began) s
""")

#: Requeue after backoff.
#:
#: `attempt` is NOT decremented here, and that is the deliberate difference from
#: graceful shutdown's `release_jobs`. A released job never started, so its
#: increment is reversed. A lost job *did* start -- it may even have completed
#: its side effects -- so it consumes a retry, and a job repeatedly lost to
#: crashing workers eventually dead-letters like any other failure instead of
#: looping forever.
#:
#: `lock_token = NULL` is the fence: the zombie's writes now match zero rows.
REVIVE_SQL = text("""
UPDATE jobs
SET status     = 'queued',
    run_at     = now() + make_interval(secs => :delay_seconds),
    claimed_by = NULL,
    claimed_at = NULL,
    started_at = NULL,
    lock_token = NULL,
    last_error = :error,
    updated_at = now()
WHERE id = :job_id AND status IN ('claimed', 'running')
""")

#: Retries exhausted while lost. Same atomic status-change-plus-dead-letter as
#: the worker's own exhaust path, so the DLQ is complete regardless of whether
#: the final failure came from the handler or from the machine underneath it.
EXHAUST_LOST_SQL = text("""
WITH dead AS (
    UPDATE jobs
    SET status       = 'dead',
        completed_at = now(),
        last_error   = :error,
        lock_token   = NULL,
        claimed_by   = NULL,
        updated_at   = now()
    WHERE id = :job_id AND status IN ('claimed', 'running')
    RETURNING id, queue_id, job_type, payload, attempt
)
INSERT INTO dead_letter_queue (
    job_id, queue_id, job_type, original_payload,
    failure_reason, total_attempts, died_at
)
SELECT d.id, d.queue_id, d.job_type, d.payload, :error, d.attempt, now()
FROM dead d
""")


async def recover_orphans(db: AsyncSession, batch_size: int = 200) -> tuple[int, int]:
    """Requeue or dead-letter every job whose owner is gone.

    Returns `(requeued, dead_lettered)`.

    The whole sweep runs in one transaction. `FIND_ORPHANS_SQL` takes row locks
    that are held until commit, so between deciding a job is orphaned and
    rewriting it no other reaper can reach the same row -- and no worker can
    claim it, because it is not in a claimable state until we put it there.
    """
    orphans = (await db.execute(FIND_ORPHANS_SQL, {"batch_size": batch_size})).all()
    if not orphans:
        return 0, 0

    requeued = dead_lettered = 0

    for o in orphans:
        reason = (
            f"Worker {o.claimed_by or 'unknown'} stopped reporting "
            f"(worker status: {o.worker_status or 'no record'}); claim "
            f"recovered by the reaper after {o.visibility_timeout_s}s "
            f"visibility timeout"
        )

        await db.execute(
            RECORD_LOST_SQL,
            {
                "job_id": o.id,
                "attempt": o.attempt,
                "worker_id": o.claimed_by,
                "started_at": o.started_at or o.claimed_at,
                "error": reason,
            },
        )

        if o.attempt >= o.max_attempts:
            await db.execute(EXHAUST_LOST_SQL, {"job_id": o.id, "error": reason})
            dead_lettered += 1
            log.error(
                "reaper.job_dead_lettered",
                job_id=str(o.id),
                attempts=o.attempt,
                max_attempts=o.max_attempts,
            )
            continue

        delay = compute_delay_seconds(
            RetryStrategy(o.strategy),
            o.attempt,
            o.base_delay_ms,
            o.max_delay_ms,
            o.jitter,
        )
        await db.execute(
            REVIVE_SQL,
            {"job_id": o.id, "delay_seconds": delay, "error": reason},
        )
        requeued += 1
        log.warning(
            "reaper.job_revived",
            job_id=str(o.id),
            attempt=o.attempt,
            max_attempts=o.max_attempts,
            retry_in_s=round(delay, 2),
        )

    await db.commit()
    return requeued, dead_lettered


# ---------------------------------------------------------------------------
# Sweep 3 -- retention
# ---------------------------------------------------------------------------
#
# `worker_heartbeats` grows at (fleet size / heartbeat interval) rows per second
# forever. Ten workers beating every ten seconds is ~86k rows a day: harmless
# for a week, a problem for a year.
#
# Deleting in bounded batches rather than in one statement keeps the transaction
# short and avoids a long lock on a table the heartbeat loop writes to every few
# seconds. At real scale the right answer is declarative partitioning by day, so
# retention becomes DROP PARTITION instead of a bulk DELETE -- noted as the
# scale-out path in DESIGN-DECISIONS.md.
#
TRIM_HEARTBEATS_SQL = text("""
DELETE FROM worker_heartbeats
WHERE id IN (
    SELECT id FROM worker_heartbeats
    WHERE beat_at < now() - make_interval(hours => :retention_hours)
    LIMIT :batch_size
)
""")


async def trim_heartbeats(
    db: AsyncSession, retention_hours: int, batch_size: int = 5000
) -> int:
    result = await db.execute(
        TRIM_HEARTBEATS_SQL,
        {"retention_hours": retention_hours, "batch_size": batch_size},
    )
    await db.commit()
    return result.rowcount or 0


async def sweep(
    db: AsyncSession,
    *,
    heartbeat_timeout_s: int,
    heartbeat_retention_hours: int,
) -> dict[str, int]:
    """Run all three sweeps. Ordering matters.

    Liveness runs first so that recovery, immediately afterwards, sees the
    workers this tick just declared dead and takes the fast path on their jobs
    instead of waiting out a five-minute visibility timeout.
    """
    dead_workers = await mark_dead_workers(db, heartbeat_timeout_s)
    await db.commit()

    requeued, dead_lettered = await recover_orphans(db)
    trimmed = await trim_heartbeats(db, heartbeat_retention_hours)

    if dead_workers or requeued or dead_lettered:
        log.info(
            "reaper.swept",
            dead_workers=dead_workers,
            requeued=requeued,
            dead_lettered=dead_lettered,
        )

    return {
        "dead_workers": dead_workers,
        "requeued": requeued,
        "dead_lettered": dead_lettered,
        "heartbeats_trimmed": trimmed,
    }
