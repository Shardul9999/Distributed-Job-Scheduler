"""The test the whole system exists to pass.

    Ten workers. One queue. No job runs twice.

Everything else in this repository is machinery in service of that sentence, so
this file asserts it directly, against a real PostgreSQL, using the *same*
`claim_jobs` the worker process calls. Testing a reimplementation of the claim
query would prove that the reimplementation is correct and say nothing at all
about the system.

Each simulated worker gets its own AsyncSession and therefore its own
connection. That detail is load-bearing: ten claimers sharing one connection
would take their locks in one transaction and never contend, and the test would
pass green while proving nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.db import Job, Queue, Worker
from packages.db.claim import claim_jobs, complete_job
from packages.db.enums import JobStatus, WorkerStatus

WORKER_COUNT = 10
JOB_COUNT = 500


async def _seed_jobs(db: AsyncSession, queue: Queue, count: int) -> None:
    """Insert `count` immediately-claimable jobs in one statement."""
    await db.execute(
        insert(Job),
        [
            {
                "queue_id": queue.id,
                "job_type": "echo",
                "payload": {"n": i},
                "status": JobStatus.QUEUED,
                "max_attempts": 3,
                "timeout_s": 30,
            }
            for i in range(count)
        ],
    )
    await db.commit()


async def _register_worker(db: AsyncSession, index: int) -> uuid.UUID:
    result = await db.execute(
        insert(Worker)
        .values(
            hostname=f"test-worker-{index}",
            pid=1000 + index,
            version="test",
            concurrency=10,
            status=WorkerStatus.ACTIVE,
        )
        .returning(Worker.id)
    )
    await db.commit()
    return result.scalar_one()


async def test_ten_workers_never_claim_the_same_job(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], scaffold: dict
) -> None:
    """500 jobs, 10 concurrent claimers, zero overlap.

    This is the assignment's explicit reliability requirement. The assertion is
    not "roughly right" or "no errors raised" -- it is that the multiset of
    claimed job ids contains no id twice.
    """
    queue = scaffold["queue"]
    await _seed_jobs(db, queue, JOB_COUNT)

    worker_ids = [await _register_worker(db, i) for i in range(WORKER_COUNT)]

    async def claimer(worker_id: uuid.UUID) -> list[uuid.UUID]:
        """One worker's whole claim loop, on its own connection."""
        mine: list[uuid.UUID] = []
        async with sessionmaker() as session:
            while True:
                claimed = await claim_jobs(session, queue.id, worker_id, batch_size=7)
                if not claimed:
                    # Empty means the queue is drained *or* every remaining row
                    # is momentarily locked by a peer. Yield and re-check once;
                    # a second empty round means genuinely drained.
                    await asyncio.sleep(0.02)
                    claimed = await claim_jobs(
                        session, queue.id, worker_id, batch_size=7
                    )
                    if not claimed:
                        return mine
                mine.extend(job["id"] for job in claimed)

    results = await asyncio.gather(*(claimer(w) for w in worker_ids))

    all_claimed = [job_id for batch in results for job_id in batch]
    duplicates = [jid for jid, n in Counter(all_claimed).items() if n > 1]

    assert not duplicates, (
        f"{len(duplicates)} job(s) were claimed by more than one worker. "
        "SKIP LOCKED is not doing its job."
    )
    assert len(all_claimed) == JOB_COUNT, (
        f"Claimed {len(all_claimed)} of {JOB_COUNT} jobs -- work was lost, "
        "which is as serious a failure as work being duplicated."
    )

    # Every job was claimed exactly once, so every job is on attempt 1. A job
    # at attempt 2 would mean it was claimed, released, and claimed again.
    attempts = (
        await db.execute(
            select(Job.attempt, func.count())
            .where(Job.queue_id == queue.id)
            .group_by(Job.attempt)
        )
    ).all()
    assert attempts == [(1, JOB_COUNT)], f"Unexpected attempt distribution: {attempts}"

    # And the fleet actually spread the work rather than one claimer winning
    # every round -- otherwise this would be a single-worker test wearing a
    # ten-worker costume.
    assert sum(1 for batch in results if batch) >= 2, (
        "Only one claimer got any work; the concurrency in this test is not real."
    )


async def test_exactly_once_execution_end_to_end(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], scaffold: dict
) -> None:
    """Claim *and complete* under contention: no job completes twice.

    The previous test proves claims are disjoint. This one carries each claim
    through to a fenced completion, which is where a naive implementation
    would still lose -- two workers can hold stale tokens for the same job and
    both write a result.
    """
    queue = scaffold["queue"]
    count = 200
    await _seed_jobs(db, queue, count)

    worker_ids = [await _register_worker(db, 100 + i) for i in range(WORKER_COUNT)]
    completions: list[uuid.UUID] = []
    lock = asyncio.Lock()

    async def run_worker(worker_id: uuid.UUID) -> None:
        async with sessionmaker() as session:
            idle_rounds = 0
            while idle_rounds < 2:
                claimed = await claim_jobs(session, queue.id, worker_id, batch_size=5)
                if not claimed:
                    idle_rounds += 1
                    await asyncio.sleep(0.02)
                    continue
                idle_rounds = 0
                for job in claimed:
                    ok = await complete_job(
                        session, job["id"], job["lock_token"], '{"ok": true}'
                    )
                    if ok:
                        async with lock:
                            completions.append(job["id"])

    await asyncio.gather(*(run_worker(w) for w in worker_ids))

    assert len(completions) == len(set(completions)), (
        "A job was completed more than once."
    )
    assert len(completions) == count

    remaining = await db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.queue_id == queue.id, Job.status != JobStatus.COMPLETED)
    )
    assert remaining == 0, f"{remaining} jobs did not reach COMPLETED"


async def _warm_pool(
    maker: async_sessionmaker[AsyncSession], n: int
) -> None:
    """Establish `n` pooled connections *before* the concurrent section.

    This is not tidiness, it is the difference between a real test and a green
    one. Opening an asyncpg connection takes long enough that a cold pool
    staggers concurrent claimers so they never actually overlap -- and a claim
    race that never overlaps is a race that never fires. The fleet-wide cap was
    broken by a factor of ten for three days behind exactly this effect: the
    first (cold) round claimed the correct 3, every warm round after it claimed
    30, and the suite only ever ran the cold one.

    In production the pool is warm and long-lived, so warm is the honest
    condition to test under.
    """

    async def touch() -> None:
        async with maker() as session:
            await session.execute(text("SELECT 1"))

    await asyncio.gather(*(touch() for _ in range(n)))


async def _in_flight(db: AsyncSession, queue_id: uuid.UUID) -> int:
    """Jobs actually held by someone, read from the database.

    Asserting on what `claim_jobs` *returned* trusts the code under test to
    report itself honestly. This counts rows.
    """
    return await db.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.queue_id == queue_id,
            Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
        )
    )


async def test_queue_concurrency_cap_is_fleet_wide(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], scaffold: dict
) -> None:
    """`max_concurrency` caps the *fleet*, not each worker.

    A per-worker cap would be trivial (a semaphore) and useless -- the point of
    the setting is to protect a downstream dependency from the whole fleet at
    once. Ten workers against a cap of 3 must yield 3 claims in total.
    """
    queue = scaffold["queue"]
    queue.max_concurrency = 3
    await db.commit()

    await _seed_jobs(db, queue, 50)
    worker_ids = [await _register_worker(db, 200 + i) for i in range(WORKER_COUNT)]
    await _warm_pool(sessionmaker, WORKER_COUNT)

    async def grab(worker_id: uuid.UUID) -> int:
        async with sessionmaker() as session:
            return len(await claim_jobs(session, queue.id, worker_id, batch_size=10))

    counts = await asyncio.gather(*(grab(w) for w in worker_ids))
    assert sum(counts) == 3, (
        f"Fleet claimed {sum(counts)} jobs against a max_concurrency of 3"
    )
    assert await _in_flight(db, queue.id) == 3


async def test_concurrency_cap_holds_with_a_warm_pool(
    db: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], scaffold: dict
) -> None:
    """Regression: the cap must hold on every round, not just the first.

    Guards the queue row lock in `CLAIM_SQL`. Remove that lock and this fails on
    round 2 with 30 jobs in flight against a cap of 3 -- every claimer reads the
    same in-flight count under READ COMMITTED, computes the same headroom, and
    takes a full allowance each. `SKIP LOCKED` keeps them on *different rows*;
    it does not make them share a *budget*.

    Several rounds, because round 1 runs against a pool that is only just warm
    and is the one condition under which the broken version passes.
    """
    project, policy = scaffold["project"], scaffold["policy"]
    cap, rounds = 3, 4
    await _warm_pool(sessionmaker, WORKER_COUNT)
    worker_ids = [await _register_worker(db, 400 + i) for i in range(WORKER_COUNT)]

    for round_no in range(rounds):
        # A fresh queue each round: jobs claimed in the previous round are still
        # in flight and would otherwise legitimately consume the next round's
        # headroom, hiding an overshoot behind a correct-looking zero.
        queue = Queue(
            project_id=project.id,
            name=f"cap-{uuid.uuid4().hex[:8]}",
            max_concurrency=cap,
            visibility_timeout_s=300,
            default_timeout_s=60,
            retry_policy_id=policy.id,
        )
        db.add(queue)
        await db.commit()
        await _seed_jobs(db, queue, 50)

        async def grab(worker_id: uuid.UUID, q_id: uuid.UUID = queue.id) -> int:
            async with sessionmaker() as session:
                return len(await claim_jobs(session, q_id, worker_id, batch_size=10))

        await asyncio.gather(*(grab(w) for w in worker_ids))

        in_flight = await _in_flight(db, queue.id)
        assert in_flight == cap, (
            f"round {round_no}: {in_flight} jobs in flight against "
            f"max_concurrency={cap} -- the fleet-wide budget was not enforced"
        )


async def test_paused_queue_yields_nothing(
    db: AsyncSession, scaffold: dict
) -> None:
    """Pause is enforced inside the claim query, not by the worker.

    That matters because a worker cannot be trusted to check: a stale replica
    running old code, or a worker that read the queue's state a second before
    an operator hit Pause, would keep claiming. The `headroom` CTE returns no
    rows for a paused queue, so the claim returns empty regardless of what the
    caller believes.
    """
    queue = scaffold["queue"]
    await _seed_jobs(db, queue, 10)
    worker_id = await _register_worker(db, 300)

    queue.is_paused = True
    await db.commit()

    assert await claim_jobs(db, queue.id, worker_id, batch_size=10) == []

    queue.is_paused = False
    await db.commit()

    assert len(await claim_jobs(db, queue.id, worker_id, batch_size=10)) == 10


async def test_priority_and_run_at_ordering(
    db: AsyncSession, scaffold: dict
) -> None:
    """Higher priority first; ties broken by the oldest `run_at`.

    This ordering is not incidental -- it matches `idx_jobs_claim` exactly, so
    PostgreSQL walks the index in order and never adds a Sort node.
    """
    queue = scaffold["queue"]
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    rows = [
        {"priority": 0, "run_at": now - timedelta(seconds=30), "tag": "low-old"},
        {"priority": 5, "run_at": now - timedelta(seconds=10), "tag": "high-new"},
        {"priority": 5, "run_at": now - timedelta(seconds=60), "tag": "high-old"},
        {"priority": -5, "run_at": now - timedelta(seconds=90), "tag": "lowest"},
    ]
    await db.execute(
        insert(Job),
        [
            {
                "queue_id": queue.id,
                "job_type": "echo",
                "payload": {"tag": r["tag"]},
                "status": JobStatus.QUEUED,
                "priority": r["priority"],
                "run_at": r["run_at"],
                "max_attempts": 3,
                "timeout_s": 30,
            }
            for r in rows
        ],
    )
    await db.commit()

    worker_id = await _register_worker(db, 400)
    claimed = await claim_jobs(db, queue.id, worker_id, batch_size=4)

    order = [job["payload"]["tag"] for job in claimed]
    assert order == ["high-old", "high-new", "low-old", "lowest"], order


async def test_future_jobs_are_not_claimable(
    db: AsyncSession, scaffold: dict
) -> None:
    """A delayed or scheduled job stays invisible until `run_at` arrives.

    One column expresses immediate, delayed and scheduled execution; this is
    the assertion that it actually does.
    """
    queue = scaffold["queue"]
    from datetime import UTC, datetime, timedelta

    await db.execute(
        insert(Job),
        [
            {
                "queue_id": queue.id,
                "job_type": "echo",
                "payload": {},
                "status": JobStatus.SCHEDULED,
                "run_at": datetime.now(UTC) + timedelta(hours=1),
                "max_attempts": 3,
                "timeout_s": 30,
            }
        ],
    )
    await db.commit()

    worker_id = await _register_worker(db, 500)
    assert await claim_jobs(db, queue.id, worker_id, batch_size=10) == []
