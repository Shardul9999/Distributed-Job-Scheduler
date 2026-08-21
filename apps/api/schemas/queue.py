"""Queue and retry-policy contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from apps.api.schemas.common import ORMModel
from packages.db.enums import RetryStrategy


# =============================================================================
# Retry policies
# =============================================================================


class RetryPolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    max_attempts: int = Field(default=3, ge=1, le=100)
    base_delay_ms: int = Field(default=1000, ge=0, le=86_400_000)
    max_delay_ms: int = Field(default=3_600_000, ge=0, le=86_400_000)
    jitter: bool = True

    @model_validator(mode="after")
    def _check_delay_ordering(self) -> RetryPolicyCreate:
        # Mirrors the ck_retry_policies_max_delay_gte_base CHECK constraint.
        # Validating here too turns a database IntegrityError into a clean 422
        # naming the offending field.
        if self.max_delay_ms < self.base_delay_ms:
            raise ValueError("max_delay_ms must be >= base_delay_ms")
        return self


class RetryPolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    strategy: RetryStrategy | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=100)
    base_delay_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    max_delay_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    jitter: bool | None = None


class RetryPolicyResponse(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    strategy: RetryStrategy
    max_attempts: int
    base_delay_ms: int
    max_delay_ms: int
    jitter: bool
    created_at: datetime


# =============================================================================
# Queues
# =============================================================================


class QueueCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    priority: int = Field(default=0, ge=-1000, le=1000)
    max_concurrency: int = Field(default=10, ge=1, le=10_000)
    visibility_timeout_s: int = Field(default=300, ge=1, le=86_400)
    default_timeout_s: int = Field(default=60, ge=1, le=86_400)
    rate_limit_per_sec: int | None = Field(default=None, ge=1, le=100_000)
    # Omit to have a default policy created alongside the queue, so a caller
    # can create a working queue in one request.
    retry_policy_id: uuid.UUID | None = None


class QueueUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    max_concurrency: int | None = Field(default=None, ge=1, le=10_000)
    visibility_timeout_s: int | None = Field(default=None, ge=1, le=86_400)
    default_timeout_s: int | None = Field(default=None, ge=1, le=86_400)
    rate_limit_per_sec: int | None = Field(default=None, ge=1, le=100_000)
    retry_policy_id: uuid.UUID | None = None


class QueueResponse(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    description: str | None
    priority: int
    max_concurrency: int
    is_paused: bool
    visibility_timeout_s: int
    default_timeout_s: int
    rate_limit_per_sec: int | None
    retry_policy_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class QueueStatsResponse(BaseModel):
    """Depth and health for one queue.

    Serves the assignment's "queue statistics" requirement and backs the
    dashboard's queue health view. Every count comes from one grouped scan
    rather than one query per status.
    """

    queue_id: uuid.UUID
    name: str
    is_paused: bool

    # Current depth, by state.
    queued: int = 0
    scheduled: int = 0
    claimed: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    dead: int = 0
    cancelled: int = 0

    #: queued + scheduled -- what is waiting to be worked.
    backlog: int = 0
    #: claimed + running -- what the fleet currently holds.
    in_flight: int = 0

    # Rolling window metrics.
    completed_last_hour: int = 0
    failed_last_hour: int = 0
    avg_duration_ms: float | None = None
    oldest_queued_age_s: float | None = None
