"""Dead-letter queue inspection and replay.

The DLQ is where a job goes when it has genuinely run out of chances. Two things
make it useful rather than just a graveyard:

    it is *complete*   the entry is written in the same transaction that marks
                       the job dead, by both the worker's exhaust path and the
                       reaper's, so no terminal failure escapes it
    it is *replayable* an operator can amend the payload and re-enqueue

Replay creates a new job and links to it. It never resurrects the original,
because the value of this table is the record of what failed -- mutating the
row to say "actually it succeeded later" would destroy exactly the thing an
incident review needs.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import settings
from apps.api.core.errors import ConflictError, NotFoundError, ValidationError
from apps.api.core.pagination import PageParams, build_page
from apps.api.schemas.dlq import ReplayRequest
from apps.api.services.ai_summary import summarize_failure
from packages.db import DeadLetterEntry, Job, Queue, RetryPolicy
from packages.db.enums import JobStatus

log = structlog.get_logger(__name__)


async def list_entries(
    db: AsyncSession,
    project_id: uuid.UUID,
    params: PageParams,
    queue_id: uuid.UUID | None = None,
    unreplayed_only: bool = False,
) -> tuple[list[DeadLetterEntry], str | None, bool]:
    """Paginated DLQ listing, newest death first.

    Keyset on `(died_at, id)` rather than `created_at`: this table has no
    `created_at`, and `died_at` is the only ordering an operator ever wants.
    `unreplayed_only` is served by the partial index `idx_dlq_unreplayed`,
    which is the dashboard's default view.
    """
    stmt = (
        select(DeadLetterEntry)
        .join(Queue, Queue.id == DeadLetterEntry.queue_id)
        .where(Queue.project_id == project_id)
    )
    if queue_id is not None:
        stmt = stmt.where(DeadLetterEntry.queue_id == queue_id)
    if unreplayed_only:
        stmt = stmt.where(DeadLetterEntry.replayed_at.is_(None))

    cursor = params.decoded_cursor
    if cursor is not None:
        stmt = stmt.where(
            tuple_(DeadLetterEntry.died_at, DeadLetterEntry.id)
            < (cursor.ts, cursor.id)
        )

    stmt = stmt.order_by(
        DeadLetterEntry.died_at.desc(), DeadLetterEntry.id.desc()
    ).limit(params.limit + 1)

    rows = list((await db.execute(stmt)).scalars().all())
    return build_page(rows, params, ts_attr="died_at")


async def get_entry(
    db: AsyncSession, project_id: uuid.UUID, entry_id: uuid.UUID
) -> DeadLetterEntry:
    stmt = (
        select(DeadLetterEntry)
        .join(Queue, Queue.id == DeadLetterEntry.queue_id)
        .where(DeadLetterEntry.id == entry_id, Queue.project_id == project_id)
    )
    entry = (await db.execute(stmt)).scalar_one_or_none()
    if entry is None:
        raise NotFoundError("Dead letter entry not found")

    # AI failure summary (bonus): generate lazily on first inspection, then
    # persist so it is computed at most once per entry. Best-effort -- if the
    # provider is unset or the call fails, ai_summary simply stays null and the
    # entry is returned exactly as it would be without the feature.
    if entry.ai_summary is None and settings.ai_summary_enabled:
        summary = await summarize_failure(
            entry.job_type, entry.failure_reason, entry.error_stack
        )
        if summary:
            entry.ai_summary = summary
            await db.commit()
            await db.refresh(entry)

    return entry


async def replay(
    db: AsyncSession,
    project_id: uuid.UUID,
    entry_id: uuid.UUID,
    overrides: ReplayRequest,
) -> tuple[DeadLetterEntry, Job]:
    """Re-enqueue a dead job as a new job, linked back to its DLQ entry.

    Refuses a second replay of the same entry. Without that check, a
    double-clicked Replay button in the dashboard is a duplicate execution of
    work that already failed once -- and the whole system exists to make
    duplicate execution impossible.
    """
    entry = await get_entry(db, project_id, entry_id)

    if entry.replayed_at is not None:
        raise ConflictError(
            f"This entry was already replayed as job {entry.replayed_job_id}"
        )

    target_queue_id = overrides.queue_id or entry.queue_id
    queue = await db.get(Queue, target_queue_id)
    if queue is None or queue.project_id != project_id:
        raise NotFoundError("Target queue not found")

    policy = await db.get(RetryPolicy, queue.retry_policy_id)
    if policy is None:
        raise ValidationError("Target queue has no retry policy")

    replayed = Job(
        queue_id=queue.id,
        job_type=entry.job_type,
        payload=overrides.payload if overrides.payload is not None else entry.original_payload,
        status=JobStatus.QUEUED,
        priority=overrides.priority if overrides.priority is not None else queue.priority,
        max_attempts=overrides.max_attempts or policy.max_attempts,
        timeout_s=queue.default_timeout_s,
    )
    db.add(replayed)
    # Flush rather than commit: the new job's id is needed to stamp the entry,
    # and both writes must land together. A replay that creates a job but fails
    # to record the link would let the same entry be replayed again.
    await db.flush()

    entry.replayed_at = func.now()
    entry.replayed_job_id = replayed.id

    await db.commit()
    await db.refresh(replayed)
    await db.refresh(entry)

    log.info(
        "dlq.replayed",
        entry_id=str(entry_id),
        original_job_id=str(entry.job_id),
        replayed_job_id=str(replayed.id),
        queue=queue.name,
    )
    return entry, replayed


async def discard(
    db: AsyncSession, project_id: uuid.UUID, entry_id: uuid.UUID
) -> None:
    """Permanently remove a DLQ entry.

    The triaged-and-rejected path: this failure has been understood and will not
    be replayed. The originating job row is left alone -- it stays `dead` and
    still appears in the job explorer, so discarding the entry clears the
    operator's queue without erasing history.
    """
    entry = await get_entry(db, project_id, entry_id)
    await db.delete(entry)
    await db.commit()
    log.info("dlq.discarded", entry_id=str(entry_id), job_id=str(entry.job_id))
