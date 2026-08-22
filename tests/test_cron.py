"""Cron scheduling: fire times, timezones, DST, and no double-firing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.scheduler import cron
from packages.db import Job, ScheduledJob


# ---------------------------------------------------------------------------
# Pure fire-time maths -- no database needed
# ---------------------------------------------------------------------------


def test_validate_rejects_nonsense() -> None:
    cron.validate("*/5 * * * *", "UTC")
    with pytest.raises(cron.CronError):
        cron.validate("not a cron", "UTC")
    with pytest.raises(cron.CronError):
        cron.validate("* * * * *", "Mars/Olympus_Mons")


def test_next_fire_time_is_utc_and_strictly_future() -> None:
    after = datetime(2026, 3, 1, 10, 30, tzinfo=UTC)
    result = cron.next_fire_time("0 * * * *", "UTC", after)
    assert result == datetime(2026, 3, 1, 11, 0, tzinfo=UTC)
    assert result > after


def test_expression_is_evaluated_in_the_schedules_timezone() -> None:
    """"09:00" means 09:00 where the schedule lives, not 09:00 UTC.

    India is UTC+5:30 year-round, so a 09:00 IST schedule must resolve to
    03:30 UTC. Getting this wrong is a five-and-a-half-hour bug that nobody
    notices until a daily report lands at the wrong time.
    """
    after = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    result = cron.next_fire_time("0 9 * * *", "Asia/Kolkata", after)
    assert result == datetime(2026, 6, 1, 3, 30, tzinfo=UTC)
    assert result.astimezone(ZoneInfo("Asia/Kolkata")).hour == 9


def test_daily_schedule_survives_a_dst_transition() -> None:
    """A schedule pinned to local 09:00 stays at local 09:00 across the change,
    which means its UTC instant moves by an hour.

    This is the whole reason the timezone is stored as an IANA name rather than
    as a fixed offset: an offset would keep the UTC instant stable and drift
    the local time, which is precisely backwards.
    """
    london = ZoneInfo("Europe/London")

    # The UK moves to BST on the last Sunday of March 2026 (29 March).
    before = cron.next_fire_time(
        "0 9 * * *", "Europe/London", datetime(2026, 3, 27, 12, 0, tzinfo=UTC)
    )
    after = cron.next_fire_time(
        "0 9 * * *", "Europe/London", datetime(2026, 3, 30, 12, 0, tzinfo=UTC)
    )

    assert before.astimezone(london).hour == 9
    assert after.astimezone(london).hour == 9
    # Local time held; the UTC hour shifted, which is the correct behaviour.
    assert before.hour == 9
    assert after.hour == 8


def test_weekday_only_schedule_skips_the_weekend() -> None:
    # Friday 2026-03-06, after the fire time.
    friday_evening = datetime(2026, 3, 6, 18, 0, tzinfo=UTC)
    nxt = cron.next_fire_time("0 9 * * 1-5", "UTC", friday_evening)
    assert nxt.weekday() == 0, "expected the next occurrence to be Monday"
    assert nxt == datetime(2026, 3, 9, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Materialisation against the database
# ---------------------------------------------------------------------------


async def _schedule(db: AsyncSession, queue_id, *, next_run_at, expr="* * * * *"):
    result = await db.execute(
        insert(ScheduledJob)
        .values(
            queue_id=queue_id,
            name=f"sched-{next_run_at.timestamp()}",
            cron_expression=expr,
            timezone="UTC",
            job_type="echo",
            payload={"from": "cron"},
            is_active=True,
            next_run_at=next_run_at,
        )
        .returning(ScheduledJob.id)
    )
    await db.commit()
    return result.scalar_one()


async def test_due_template_produces_exactly_one_job(
    db: AsyncSession, scaffold: dict
) -> None:
    queue = scaffold["queue"]
    fire_at = datetime.now(UTC) - timedelta(seconds=5)
    schedule_id = await _schedule(db, queue.id, next_run_at=fire_at)

    assert await cron.materialize_due(db) == 1

    jobs = (
        await db.execute(select(Job).where(Job.scheduled_job_id == schedule_id))
    ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].payload == {"from": "cron"}
    assert jobs[0].job_type == "echo"

    schedule = await db.get(ScheduledJob, schedule_id, populate_existing=True)
    assert schedule.last_run_at is not None
    assert schedule.next_run_at > datetime.now(UTC)


async def test_a_second_tick_does_not_refire_the_same_occurrence(
    db: AsyncSession, scaffold: dict
) -> None:
    """Ticking again immediately creates nothing.

    `next_run_at` has already been advanced, so the template is no longer due.
    Belt and braces underneath that, the occurrence's idempotency key would
    reject a duplicate insert at the index level even if the advance were lost.
    """
    queue = scaffold["queue"]
    schedule_id = await _schedule(
        db, queue.id, next_run_at=datetime.now(UTC) - timedelta(seconds=5)
    )

    assert await cron.materialize_due(db) == 1
    assert await cron.materialize_due(db) == 0

    count = await db.scalar(
        select(func.count()).select_from(Job).where(Job.scheduled_job_id == schedule_id)
    )
    assert count == 1


async def test_occurrence_key_makes_double_firing_impossible(
    db: AsyncSession, scaffold: dict
) -> None:
    """Rewinding `next_run_at` to an occurrence already fired inserts nothing.

    This simulates the crash window: the scheduler inserted the job and died
    before advancing the template. On restart it sees the same occurrence due
    again -- and the partial unique index on (queue_id, idempotency_key)
    silently refuses the duplicate rather than the fleet running the work twice.
    """
    queue = scaffold["queue"]
    fire_at = datetime.now(UTC) - timedelta(seconds=5)
    schedule_id = await _schedule(db, queue.id, next_run_at=fire_at)

    assert await cron.materialize_due(db) == 1

    schedule = await db.get(ScheduledJob, schedule_id, populate_existing=True)
    schedule.next_run_at = fire_at  # as if the advance never committed
    await db.commit()

    assert await cron.materialize_due(db) == 0, "the same occurrence fired twice"

    count = await db.scalar(
        select(func.count()).select_from(Job).where(Job.scheduled_job_id == schedule_id)
    )
    assert count == 1


async def test_inactive_schedules_are_never_fired(
    db: AsyncSession, scaffold: dict
) -> None:
    queue = scaffold["queue"]
    schedule_id = await _schedule(
        db, queue.id, next_run_at=datetime.now(UTC) - timedelta(minutes=10)
    )
    schedule = await db.get(ScheduledJob, schedule_id)
    schedule.is_active = False
    await db.commit()

    assert await cron.materialize_due(db) == 0


async def test_missed_occurrences_are_skipped_not_backfilled(
    db: AsyncSession, scaffold: dict
) -> None:
    """An hour of downtime on a per-minute schedule produces one job, not sixty.

    The alternative -- replaying every missed occurrence -- turns a scheduler
    restart into a self-inflicted denial of service against the fleet.
    """
    queue = scaffold["queue"]
    schedule_id = await _schedule(
        db,
        queue.id,
        next_run_at=datetime.now(UTC) - timedelta(hours=1),
        expr="* * * * *",
    )

    created = await cron.materialize_due(db)
    assert created == 1

    count = await db.scalar(
        select(func.count()).select_from(Job).where(Job.scheduled_job_id == schedule_id)
    )
    assert count == 1

    schedule = await db.get(ScheduledJob, schedule_id, populate_existing=True)
    assert schedule.next_run_at > datetime.now(UTC), (
        "next_run_at was advanced by one interval from the missed slot instead "
        "of forward past now(), which would replay the backlog one tick at a time"
    )
