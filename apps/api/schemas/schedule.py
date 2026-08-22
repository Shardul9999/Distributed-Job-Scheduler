"""Recurring schedule (cron template) contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from apps.api.schemas.common import ORMModel
from apps.scheduler.cron import CronError, validate


def _validate_cron(expression: str, timezone: str) -> None:
    """Reject an unparseable schedule at write time.

    The scheduler discovers a bad expression only when the template comes due,
    which could be days later and at an hour when nobody is reading logs.
    Validating on the write path turns that into an immediate 422 naming the
    field -- using the *same* function the scheduler will use, so the two can
    never disagree about what is valid.
    """
    try:
        validate(expression, timezone)
    except CronError as exc:
        raise ValueError(str(exc)) from exc


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    cron_expression: str = Field(
        min_length=1,
        max_length=100,
        examples=["*/5 * * * *", "0 9 * * 1-5"],
        description="Standard five-field cron expression.",
    )
    timezone: str = Field(
        default="UTC",
        max_length=64,
        examples=["UTC", "Asia/Kolkata"],
        description="IANA timezone name the expression is evaluated in.",
    )
    job_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-1000, le=1000)
    is_active: bool = True
    #: First fire time. Omitted means "the next occurrence from now", which is
    #: what an operator almost always means by "start this schedule".
    start_at: datetime | None = None

    @model_validator(mode="after")
    def _check_cron(self) -> ScheduleCreate:
        _validate_cron(self.cron_expression, self.timezone)
        return self


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    job_type: str | None = Field(default=None, min_length=1, max_length=100)
    payload: dict[str, Any] | None = None
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    is_active: bool | None = None

    @field_validator("cron_expression")
    @classmethod
    def _shape(cls, v: str | None) -> str | None:
        # Full validation needs the timezone too, which may live on the stored
        # row rather than in this payload; the service re-validates the merged
        # pair. This catches the obvious case early with a better message.
        if v is not None:
            _validate_cron(v, "UTC")
        return v


class ScheduleResponse(ORMModel):
    id: uuid.UUID
    queue_id: uuid.UUID
    name: str
    cron_expression: str
    timezone: str
    job_type: str
    payload: dict[str, Any]
    priority: int
    is_active: bool
    last_run_at: datetime | None
    next_run_at: datetime
    created_at: datetime
    updated_at: datetime


class ScheduleTriggerResponse(BaseModel):
    """Result of firing a schedule by hand.

    `next_run_at` is deliberately unchanged by a manual trigger: an operator
    testing a schedule should not shift its recurring timetable.
    """

    schedule_id: uuid.UUID
    job_id: uuid.UUID
    next_run_at: datetime
