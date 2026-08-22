"""Atomic job claiming and result recording.

This module is the core of the whole system. Everything else -- the API, the
dashboard, the scheduler -- is supporting apparatus around the guarantee made
here: **a job is executed by exactly one worker at a time.**

The guarantee is enforced by PostgreSQL, not by application code, using
`FOR UPDATE ... SKIP LOCKED`. That matters: there is no distributed consensus
to get wrong, no lock server to fail over, and no window in which two workers
both believe they own the same row.

Kept in `packages/db` rather than in the worker so the API (manual retry, DLQ
replay) and the test suite can use the identical statements the worker uses --
a concurrency test that exercises a reimplementation proves nothing.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# THE CLAIM QUERY
# ---------------------------------------------------------------------------
#
# One statement does all of: pick candidates, lock them, mark them claimed,
# issue fencing tokens, and return the work. Because it is a single statement it
# is a single transaction -- a worker that dies midway rolls back cleanly and
# the jobs stay claimable by someone else.
#
# Reading it outside-in:
#
#   headroom   How many more jobs from this queue may run right now, given the
#              queue's FLEET-WIDE max_concurrency and what is already in flight
#              across every worker. Yields no rows at all when the queue is
#              paused, which makes the whole claim return empty -- that is how
#              pause/resume is enforced.
#
#   claimable  The candidate rows. Sorted by priority DESC, run_at ASC, which
#              matches `idx_jobs_claim` exactly so PostgreSQL walks the index in
#              order and never sorts.
#
#              FOR UPDATE OF j   -> lock the job rows (only `jobs`; `headroom`
#                                   is a CTE and cannot be locked)
#              SKIP LOCKED       -> step over rows another transaction already
#                                   holds instead of blocking on them. This is
#                                   the difference between ten workers running
#                                   concurrently and ten workers queueing behind
#                                   each other.
#
#   UPDATE     Transitions the locked rows to `claimed`, stamps the owner, and
#              increments `attempt`. `lock_token` is regenerated on every claim:
#              a worker must present its token to write a result, so if the
#              reaper has since revived this job and given it to someone else,
#              the old worker's write matches zero rows and its stale result is
#              discarded rather than overwriting the live attempt.
#
# The `h.slots > 0` predicate combined with the comma join means an empty
# `headroom` (paused queue, or no free slots) produces no candidates at all --
# so the LIMIT expression is never reached with a NULL.
#
CLAIM_SQL = text("""
WITH headroom AS (
    SELECT
        q.id,
        GREATEST(
            q.max_concurrency - (
                SELECT count(*)
                FROM jobs r
                WHERE r.queue_id = q.id
                  AND r.status IN ('claimed', 'running')
            ),
            0
        ) AS slots
    FROM queues q
    WHERE q.id = :queue_id
      AND q.is_paused = false
),
claimable AS (
    SELECT j.id
    FROM jobs j, headroom h
    WHERE j.queue_id = :queue_id
      AND j.status IN ('queued', 'scheduled')
      AND j.run_at <= now()
      AND h.slots > 0
    ORDER BY j.priority DESC, j.run_at ASC
    FOR UPDATE OF j SKIP LOCKED
    LIMIT LEAST(:batch_size, (SELECT slots FROM headroom))
)
UPDATE jobs j
SET status     = 'claimed',
    claimed_by = :worker_id,
    claimed_at = now(),
    lock_token = gen_random_uuid(),
    attempt    = j.attempt + 1,
    updated_at = now()
FROM claimable c
WHERE j.id = c.id
RETURNING
    j.id, j.queue_id, j.job_type, j.payload, j.attempt, j.max_attempts,
    j.timeout_s, j.lock_token, j.priority, j.run_at
""")


async def claim_jobs(
    db: AsyncSession,
    queue_id: uuid.UUID,
    worker_id: uuid.UUID,
    batch_size: int,
) -> list[dict[str, Any]]:
    """Atomically claim up to `batch_size` jobs from one queue.

    Claims are per-queue rather than across all of a worker's queues in one
    statement. A single statement spanning queues would need a window function
    to apply each queue's concurrency cap, and PostgreSQL does not permit
    `FOR UPDATE` alongside window functions. Looping over a worker's two or
    three queues costs a handful of indexed queries and keeps the SQL provably
    correct -- documented as a deliberate trade-off in DESIGN-DECISIONS.md.

    Returns claimed jobs as plain dicts. Deliberately not ORM objects: the
    worker holds these across `await` boundaries while executing, and detached
    ORM instances attempting a lazy refresh there are a well-known source of
    async session errors.
    """
    result = await db.execute(
        CLAIM_SQL,
        {"queue_id": queue_id, "worker_id": worker_id, "batch_size": batch_size},
    )
    rows = result.mappings().all()
    await db.commit()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Result recording -- every statement below is fenced by lock_token
# ---------------------------------------------------------------------------

MARK_RUNNING_SQL = text("""
UPDATE jobs
SET status = 'running', started_at = now(), updated_at = now()
WHERE id = :job_id AND lock_token = :lock_token AND status = 'claimed'
RETURNING id
""")

COMPLETE_SQL = text("""
UPDATE jobs
SET status       = 'completed',
    completed_at = now(),
    result       = CAST(:result AS jsonb),
    last_error   = NULL,
    lock_token   = NULL,
    updated_at   = now()
WHERE id = :job_id AND lock_token = :lock_token
RETURNING id
""")

#: Transient failure: schedule another attempt.
#: `run_at` moves into the future by the backoff delay, and status returns to
#: `queued` so the partial claim index picks the row up again once due.
RETRY_SQL = text("""
UPDATE jobs
SET status     = 'queued',
    run_at     = now() + make_interval(secs => :delay_seconds),
    last_error = :error,
    claimed_by = NULL,
    claimed_at = NULL,
    started_at = NULL,
    lock_token = NULL,
    updated_at = now()
WHERE id = :job_id AND lock_token = :lock_token
RETURNING id
""")

#: Terminal failure: attempts exhausted.
#:
#: The status change and the dead_letter_queue insert are one statement, and
#: therefore one transaction. Written as two statements there would be a window
#: -- a worker crash, a connection reset -- in which a job is terminally `dead`
#: with no dead-letter record, which is the one state an operator can neither
#: see nor replay. Chaining them through a CTE makes that state unreachable.
#:
#: The DLQ row *copies* the payload rather than joining to it, so the record
#: stays readable after a retention policy prunes the originating job.
EXHAUST_SQL = text("""
WITH dead AS (
    UPDATE jobs
    SET status       = 'dead',
        completed_at = now(),
        last_error   = :error,
        lock_token   = NULL,
        updated_at   = now()
    WHERE id = :job_id AND lock_token = :lock_token
    RETURNING id, queue_id, job_type, payload, attempt
)
INSERT INTO dead_letter_queue (
    job_id, queue_id, job_type, original_payload,
    failure_reason, error_stack, total_attempts, died_at
)
SELECT d.id, d.queue_id, d.job_type, d.payload,
       :error, :error_stack, d.attempt, now()
FROM dead d
RETURNING id
""")

#: Graceful shutdown: hand work back rather than holding it until the
#: visibility timeout expires. A drained worker's jobs become immediately
#: claimable by the rest of the fleet.
RELEASE_SQL = text("""
UPDATE jobs
SET status     = 'queued',
    claimed_by = NULL,
    claimed_at = NULL,
    started_at = NULL,
    lock_token = NULL,
    attempt    = GREATEST(attempt - 1, 0),
    updated_at = now()
WHERE id = ANY(:job_ids) AND status IN ('claimed', 'running')
""")


async def mark_running(
    db: AsyncSession, job_id: uuid.UUID, lock_token: uuid.UUID
) -> bool:
    """Transition claimed -> running. False if the claim is no longer ours."""
    row = await db.execute(
        MARK_RUNNING_SQL, {"job_id": job_id, "lock_token": lock_token}
    )
    ok = row.first() is not None
    await db.commit()
    return ok


async def complete_job(
    db: AsyncSession, job_id: uuid.UUID, lock_token: uuid.UUID, result_json: str
) -> bool:
    """Record success. False means the token was stale and the write was
    correctly rejected -- the caller must not treat that as an error."""
    row = await db.execute(
        COMPLETE_SQL,
        {"job_id": job_id, "lock_token": lock_token, "result": result_json},
    )
    ok = row.first() is not None
    await db.commit()
    return ok


async def retry_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    lock_token: uuid.UUID,
    delay_seconds: float,
    error: str,
) -> bool:
    """Schedule another attempt after `delay_seconds`."""
    row = await db.execute(
        RETRY_SQL,
        {
            "job_id": job_id,
            "lock_token": lock_token,
            "delay_seconds": delay_seconds,
            "error": error[:8000],
        },
    )
    ok = row.first() is not None
    await db.commit()
    return ok


async def exhaust_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    lock_token: uuid.UUID,
    error: str,
    error_stack: str | None = None,
) -> bool:
    """Mark a job permanently dead and dead-letter it, atomically.

    Returns False when the fencing token no longer matches. Note what that
    means for the CTE: the `dead` arm produces no rows, so the INSERT selects
    from an empty set and writes nothing. A zombie worker cannot dead-letter a
    job that has already been revived and handed to someone else.
    """
    row = await db.execute(
        EXHAUST_SQL,
        {
            "job_id": job_id,
            "lock_token": lock_token,
            "error": error[:8000],
            "error_stack": error_stack[:16000] if error_stack else None,
        },
    )
    ok = row.first() is not None
    await db.commit()
    return ok


async def release_jobs(db: AsyncSession, job_ids: list[uuid.UUID]) -> None:
    """Return unfinished jobs to the queue during graceful shutdown.

    `attempt` is decremented because the claim incremented it optimistically
    and this attempt never actually ran. Without this, a rolling restart would
    silently consume every job's retry budget.
    """
    if not job_ids:
        return
    await db.execute(RELEASE_SQL, {"job_ids": job_ids})
    await db.commit()
