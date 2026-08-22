"""Cron materialisation: turning recurring templates into concrete jobs.

A `scheduled_jobs` row is a *template*. It is never claimed, never executed, and
never fails. Each time it comes due the scheduler inserts a real `jobs` row that
points back at it via `scheduled_job_id`, then advances the template's
`next_run_at`.

Keeping the two apart is what makes "this schedule has fired 400 times, 3 of
them failed" an ordinary query rather than a special case: the 400 instances are
just jobs, and every piece of machinery that already exists -- retries, the DLQ,
the job explorer, execution history -- applies to them unchanged.

Two decisions worth defending:

**Timezones are per schedule, and stored as IANA names.** "Every weekday at
09:00" is a different UTC instant in January than in July. Storing a fixed UTC
offset would silently drift by an hour twice a year; storing `Europe/London` and
resolving it at each fire keeps the schedule correct across DST transitions.

**Missed runs are skipped, not backfilled.** If the scheduler is down for an
hour, a `* * * * *` schedule has 60 unfired occurrences. Materialising all 60 on
restart would hand the fleet an instant backlog of identical work -- almost
never what an operator wants from "run this every minute". We fire once and
advance to the next occurrence after `now()`. The alternative (bounded catch-up)
is noted in DESIGN-DECISIONS.md as the configurable extension.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from croniter import croniter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("scheduler.cron")


class CronError(ValueError):
    """An invalid cron expression or timezone. Raised at write time, not fire
    time -- a schedule that cannot be parsed must be rejected by the API rather
    than discovered by the scheduler at 3am."""


def validate(cron_expression: str, timezone: str) -> None:
    """Reject a schedule the scheduler would later choke on."""
    if not croniter.is_valid(cron_expression):
        raise CronError(f"'{cron_expression}' is not a valid cron expression")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"'{timezone}' is not a known IANA timezone") from exc


def next_fire_time(
    cron_expression: str, timezone: str, after: datetime | None = None
) -> datetime:
    """The next occurrence strictly after `after`, returned in UTC.

    The expression is evaluated in the schedule's own timezone and only then
    converted back to UTC. Evaluating it directly against a UTC clock would make
    "09:00 in Mumbai" mean 09:00 UTC, and would move every schedule by an hour
    whenever its region changed offset.
    """
    tz = ZoneInfo(timezone)
    reference = (after or datetime.now(UTC)).astimezone(tz)
    nxt: datetime = croniter(cron_expression, reference).get_next(datetime)
    return nxt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------
#
# `idx_sched_due (next_run_at) WHERE is_active` serves this: inactive schedules
# are not in the index at all, so pausing a schedule makes it free to skip
# rather than merely cheap.
#
# SKIP LOCKED is belt and braces. The advisory lock in main.py already means
# only one scheduler materialises at a time; this makes a second one -- a
# manual trigger from the API, an operator running a backfill script -- safe
# rather than merely unlikely.
#
FIND_DUE_SQL = text("""
SELECT s.id,
       s.queue_id,
       s.name,
       s.cron_expression,
       s.timezone,
       s.job_type,
       s.payload,
       s.priority,
       s.next_run_at,
       q.default_timeout_s,
       rp.max_attempts
FROM scheduled_jobs s
JOIN queues q          ON q.id  = s.queue_id
JOIN retry_policies rp ON rp.id = q.retry_policy_id
WHERE s.is_active
  AND s.next_run_at <= now()
ORDER BY s.next_run_at
LIMIT :batch_size
FOR UPDATE OF s SKIP LOCKED
""")

#: The instance insert.
#:
#: `idempotency_key` is set to `cron:<schedule id>:<intended fire time>`, which
#: makes double-firing impossible at the database level rather than by
#: convention: the partial unique index `idx_jobs_idempotency` rejects the
#: second insert for a given occurrence. `ON CONFLICT DO NOTHING` turns that
#: rejection into a no-op, so a scheduler that crashes between the insert and
#: the `next_run_at` advance re-runs the tick harmlessly on restart.
#:
#: This is the same mechanism that makes a retried POST /jobs safe -- reused
#: rather than reinvented.
INSERT_INSTANCE_SQL = text("""
INSERT INTO jobs (
    queue_id, job_type, payload, status, priority,
    max_attempts, timeout_s, run_at, idempotency_key, scheduled_job_id
)
VALUES (
    :queue_id, :job_type, CAST(:payload AS jsonb), 'queued', :priority,
    :max_attempts, :timeout_s, :run_at, :idempotency_key, :scheduled_job_id
)
ON CONFLICT (queue_id, idempotency_key) WHERE idempotency_key IS NOT NULL
DO NOTHING
RETURNING id
""")

ADVANCE_SQL = text("""
UPDATE scheduled_jobs
SET last_run_at = :fired_at,
    next_run_at = :next_run_at,
    updated_at  = now()
WHERE id = :id
""")

#: A schedule whose expression or timezone no longer parses is deactivated
#: rather than retried forever. Without this the tick loop would log the same
#: exception every second until someone noticed.
DEACTIVATE_SQL = text("""
UPDATE scheduled_jobs
SET is_active = false, updated_at = now()
WHERE id = :id
""")


async def materialize_due(db: AsyncSession, batch_size: int = 100) -> int:
    """Fire every template that has come due. Returns the number of jobs created.

    The select's row locks are held for the whole transaction, so the insert and
    the `next_run_at` advance cannot interleave with another scheduler's view of
    the same template.
    """
    due = (await db.execute(FIND_DUE_SQL, {"batch_size": batch_size})).all()
    if not due:
        return 0

    created = 0
    now = datetime.now(UTC)

    for s in due:
        try:
            upcoming = next_fire_time(s.cron_expression, s.timezone, now)
        except Exception as exc:  # noqa: BLE001 - a bad expression must not stall the loop
            await db.execute(DEACTIVATE_SQL, {"id": s.id})
            log.error(
                "scheduler.schedule_deactivated",
                schedule_id=str(s.id),
                name=s.name,
                cron=s.cron_expression,
                timezone=s.timezone,
                error=str(exc),
            )
            continue

        # Keyed on the *intended* fire time, not on now(), so the key is
        # deterministic for the occurrence rather than for the moment we
        # happened to notice it.
        fired_at = s.next_run_at
        row = await db.execute(
            INSERT_INSTANCE_SQL,
            {
                "queue_id": s.queue_id,
                "job_type": s.job_type,
                "payload": json.dumps(s.payload or {}),
                "priority": s.priority,
                "max_attempts": s.max_attempts,
                "timeout_s": s.default_timeout_s,
                "run_at": fired_at,
                "idempotency_key": f"cron:{s.id}:{fired_at.isoformat()}",
                "scheduled_job_id": s.id,
            },
        )
        job_id = row.scalar_one_or_none()

        await db.execute(
            ADVANCE_SQL,
            {"id": s.id, "fired_at": fired_at, "next_run_at": upcoming},
        )

        if job_id is not None:
            created += 1
            log.info(
                "scheduler.cron_fired",
                schedule_id=str(s.id),
                name=s.name,
                job_id=str(job_id),
                fired_at=fired_at.isoformat(),
                next_run_at=upcoming.isoformat(),
            )
        else:
            # The occurrence already exists. Expected after a crash between the
            # insert and the advance; the advance below makes it self-healing.
            log.info(
                "scheduler.cron_already_fired",
                schedule_id=str(s.id),
                fired_at=fired_at.isoformat(),
            )

    await db.commit()
    return created
