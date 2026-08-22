"""Retry backoff curves, and the dead-letter guarantee.

The backoff tests are pure arithmetic and need no database. The DLQ tests do,
because the property being asserted is transactional: a job cannot be `dead`
without a dead-letter row, and no sequence of failures or stale writes can
produce one.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.db import DeadLetterEntry, Job, Worker
from packages.db.claim import claim_jobs, exhaust_job, retry_job
from packages.db.enums import JobStatus, RetryStrategy, WorkerStatus
from packages.retry import compute_delay_seconds


# ---------------------------------------------------------------------------
# Backoff curves
# ---------------------------------------------------------------------------


def test_fixed_delay_does_not_grow() -> None:
    for attempt in (1, 2, 5, 20):
        assert (
            compute_delay_seconds(RetryStrategy.FIXED, attempt, 1000, 60_000, False)
            == 1.0
        )


def test_linear_backoff_grows_by_the_base_each_attempt() -> None:
    delays = [
        compute_delay_seconds(RetryStrategy.LINEAR, n, 1000, 600_000, False)
        for n in (1, 2, 3, 4)
    ]
    assert delays == [1.0, 2.0, 3.0, 4.0]


def test_exponential_backoff_doubles() -> None:
    delays = [
        compute_delay_seconds(RetryStrategy.EXPONENTIAL, n, 1000, 600_000, False)
        for n in (1, 2, 3, 4, 5)
    ]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_every_strategy_respects_the_ceiling() -> None:
    """Without a cap, exponential backoff on a job with max_attempts=20 would
    schedule its last retry roughly six days out."""
    for strategy in RetryStrategy:
        delay = compute_delay_seconds(strategy, 30, 1000, 30_000, jitter=False)
        assert delay <= 30.0, f"{strategy} exceeded max_delay_ms"


def test_large_attempt_numbers_do_not_explode() -> None:
    """`2 ** attempt` with an unbounded attempt is an arbitrarily large Python
    integer, computed in full only to be clamped. The exponent is capped first."""
    delay = compute_delay_seconds(
        RetryStrategy.EXPONENTIAL, 10_000, 1000, 60_000, jitter=False
    )
    assert delay == 60.0


def test_full_jitter_stays_within_the_window_and_actually_varies() -> None:
    """Full jitter spreads a thundering herd across [0, delay].

    Both halves matter: a jitter that could exceed the window would break the
    max-delay contract, and one that returned a constant would not spread
    anything.
    """
    samples = [
        compute_delay_seconds(RetryStrategy.FIXED, 1, 10_000, 10_000, jitter=True)
        for _ in range(200)
    ]
    assert all(0.0 <= s <= 10.0 for s in samples)
    assert len(set(samples)) > 100, "jitter is not random"


# ---------------------------------------------------------------------------
# The dead-letter guarantee
# ---------------------------------------------------------------------------


async def _claimed_job(db: AsyncSession, queue_id: uuid.UUID, max_attempts: int = 1):
    worker_id = (
        await db.execute(
            insert(Worker)
            .values(
                hostname="dlq-test",
                pid=1,
                version="test",
                concurrency=1,
                status=WorkerStatus.ACTIVE,
            )
            .returning(Worker.id)
        )
    ).scalar_one()
    await db.execute(
        insert(Job).values(
            queue_id=queue_id,
            job_type="fail",
            payload={"why": "always"},
            status=JobStatus.QUEUED,
            max_attempts=max_attempts,
            timeout_s=30,
        )
    )
    await db.commit()
    claimed = await claim_jobs(db, queue_id, worker_id, batch_size=1)
    return claimed[0]


async def test_exhausting_a_job_writes_its_dead_letter_row(
    db: AsyncSession, scaffold: dict
) -> None:
    """Dead and dead-lettered are one transaction, so they cannot disagree."""
    job = await _claimed_job(db, scaffold["queue"].id)

    ok = await exhaust_job(
        db, job["id"], job["lock_token"], "RuntimeError: always fails", "Traceback..."
    )
    assert ok is True

    row = await db.get(Job, job["id"], populate_existing=True)
    assert row.status is JobStatus.DEAD
    assert row.lock_token is None

    entry = (
        await db.execute(
            select(DeadLetterEntry).where(DeadLetterEntry.job_id == job["id"])
        )
    ).scalar_one()
    assert entry.total_attempts == 1
    assert entry.original_payload == {"why": "always"}
    assert entry.failure_reason.startswith("RuntimeError")
    assert entry.error_stack == "Traceback..."
    assert entry.queue_id == scaffold["queue"].id
    assert entry.replayed_at is None


async def test_a_stale_token_can_neither_kill_a_job_nor_dead_letter_it(
    db: AsyncSession, scaffold: dict
) -> None:
    """The fence protects the DLQ too.

    Without this, a zombie worker could dead-letter a job that a live worker is
    successfully running -- and the operator would be triaging a failure that
    never happened.
    """
    job = await _claimed_job(db, scaffold["queue"].id)
    stale = uuid.uuid4()

    assert await exhaust_job(db, job["id"], stale, "from a zombie") is False

    row = await db.get(Job, job["id"], populate_existing=True)
    assert row.status is JobStatus.CLAIMED

    count = await db.scalar(
        select(func.count())
        .select_from(DeadLetterEntry)
        .where(DeadLetterEntry.job_id == job["id"])
    )
    assert count == 0, "a stale write created a phantom dead-letter entry"


async def test_retry_returns_the_job_with_its_run_at_pushed_out(
    db: AsyncSession, scaffold: dict
) -> None:
    """A retried job is claimable again, but not yet."""
    job = await _claimed_job(db, scaffold["queue"].id, max_attempts=5)

    assert await retry_job(db, job["id"], job["lock_token"], 60.0, "boom") is True

    row = await db.get(Job, job["id"], populate_existing=True)
    assert row.status is JobStatus.QUEUED
    assert row.claimed_by is None
    assert row.lock_token is None
    assert row.last_error == "boom"

    # Not due yet, so no worker can pick it up early.
    worker_id = (
        await db.execute(
            insert(Worker)
            .values(
                hostname="too-eager",
                pid=2,
                version="test",
                concurrency=1,
                status=WorkerStatus.ACTIVE,
            )
            .returning(Worker.id)
        )
    ).scalar_one()
    await db.commit()
    assert await claim_jobs(db, scaffold["queue"].id, worker_id, batch_size=5) == []
