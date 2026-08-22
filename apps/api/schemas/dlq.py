"""Dead-letter queue contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from apps.api.schemas.common import ORMModel


class DeadLetterResponse(ORMModel):
    """One permanently-failed job, as an operator needs to see it.

    The payload is the copy taken at death, not a join back to the job row. A
    DLQ entry has to stay readable and replayable after a retention policy has
    pruned the original.
    """

    id: uuid.UUID
    job_id: uuid.UUID
    queue_id: uuid.UUID
    job_type: str
    original_payload: dict[str, Any]
    failure_reason: str
    error_stack: str | None
    total_attempts: int
    died_at: datetime
    replayed_at: datetime | None
    replayed_job_id: uuid.UUID | None
    ai_summary: str | None


class ReplayRequest(BaseModel):
    """Optional overrides applied when re-enqueuing a dead job.

    A job usually dead-letters because something about it was wrong. Replaying
    it byte-for-byte just kills it again, so the operator can amend the payload
    or raise the attempt budget as part of the replay.
    """

    payload: dict[str, Any] | None = Field(
        default=None,
        description="Replaces the original payload. Omit to replay unchanged.",
    )
    max_attempts: int | None = Field(default=None, ge=1, le=100)
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    queue_id: uuid.UUID | None = Field(
        default=None,
        description="Re-enqueue onto a different queue in the same project.",
    )


class ReplayResponse(BaseModel):
    """The replay creates a *new* job and links to it.

    The original stays `dead` and the DLQ entry stays in place, now stamped
    with `replayed_at`. Resurrecting the original row instead would destroy the
    forensic record of what failed and how often -- exactly the thing a dead
    letter queue exists to preserve.
    """

    dlq_entry_id: uuid.UUID
    original_job_id: uuid.UUID
    replayed_job_id: uuid.UUID
    queue_id: uuid.UUID
