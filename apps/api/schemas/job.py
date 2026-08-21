"""Job contracts.

One creation schema covers immediate, delayed and scheduled jobs. The
assignment lists them as separate job kinds, but they differ only in the value
of `run_at` -- modelling them as three types would add API surface without
adding capability.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, model_validator

from apps.api.schemas.common import ORMModel
from packages.db.enums import ExecutionStatus, JobStatus, LogLevel


class JobCreate(BaseModel):
    job_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)

    priority: int | None = Field(default=None, ge=-1000, le=1000)
    max_attempts: int | None = Field(default=None, ge=1, le=100)
    timeout_s: int | None = Field(default=None, ge=1, le=86_400)

    # Exactly one of these, or neither (which means "run immediately").
    run_at: datetime | None = None
    delay_seconds: int | None = Field(default=None, ge=0, le=31_536_000)

    idempotency_key: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _resolve_schedule(self) -> JobCreate:
        if self.run_at is not None and self.delay_seconds is not None:
            raise ValueError("Provide run_at or delay_seconds, not both")

        if self.delay_seconds is not None:
            self.run_at = datetime.now(UTC) + timedelta(seconds=self.delay_seconds)

        # A naive datetime is ambiguous across the API, the worker and the
        # database. Assume UTC rather than guessing the caller's zone.
        if self.run_at is not None and self.run_at.tzinfo is None:
            self.run_at = self.run_at.replace(tzinfo=UTC)

        return self

    @property
    def effective_run_at(self) -> datetime:
        return self.run_at or datetime.now(UTC)


class BatchJobCreate(BaseModel):
    """Submit many jobs in one request and one transaction.

    Capped at 1000. An unbounded batch would hold a write transaction open long
    enough to bloat the WAL and block autovacuum on the hottest table in the
    system.
    """

    jobs: list[JobCreate] = Field(min_length=1, max_length=1000)


class BatchJobResponse(BaseModel):
    batch_id: uuid.UUID
    created: int
    job_ids: list[uuid.UUID]


class JobResponse(ORMModel):
    id: uuid.UUID
    queue_id: uuid.UUID
    job_type: str
    payload: dict[str, Any]
    status: JobStatus
    priority: int
    attempt: int
    max_attempts: int
    timeout_s: int

    run_at: datetime
    claimed_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    claimed_by: uuid.UUID | None

    idempotency_key: str | None
    batch_id: uuid.UUID | None
    scheduled_job_id: uuid.UUID | None
    depends_on: list[uuid.UUID] | None

    last_error: str | None
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class JobExecutionResponse(ORMModel):
    """One attempt. The immutable history behind a job's current state."""

    id: uuid.UUID
    job_id: uuid.UUID
    attempt_number: int
    worker_id: uuid.UUID | None
    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    error_message: str | None
    error_stack: str | None
    output: dict[str, Any] | None


class JobLogResponse(ORMModel):
    id: int
    job_id: uuid.UUID
    execution_id: uuid.UUID | None
    level: LogLevel
    message: str
    logged_at: datetime


class JobDetailResponse(JobResponse):
    """Job plus its full attempt history, for the dashboard's detail drawer."""

    executions: list[JobExecutionResponse] = []


class JobFilters(BaseModel):
    """Filters for the Job Explorer.

    Every combination here is served by `idx_jobs_explorer`
    (queue_id, status, created_at DESC) or by a primary-key lookup.
    """

    queue_id: uuid.UUID | None = None
    status: JobStatus | None = None
    job_type: str | None = None
    batch_id: uuid.UUID | None = None
