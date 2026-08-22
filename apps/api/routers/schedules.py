"""Recurring schedule endpoints: cron template CRUD and manual trigger."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from apps.api.core.deps import AccessibleProject, CurrentUser, DbSession
from apps.api.routers.queues import _authorized_queue
from apps.api.schemas.common import ErrorResponse, MessageResponse
from apps.api.schemas.schedule import (
    ScheduleCreate,
    ScheduleResponse,
    ScheduleTriggerResponse,
    ScheduleUpdate,
)
from apps.api.services import schedule_service

router = APIRouter(tags=["schedules"])

QueueId = Annotated[uuid.UUID, Path(description="Queue id")]
ScheduleId = Annotated[uuid.UUID, Path(description="Schedule id")]

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}


@router.post(
    "/queues/{queue_id}/schedules",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_NOT_FOUND,
    summary="Create a recurring schedule",
    description=(
        "Creates a cron *template*. The scheduler materialises a concrete job "
        "from it each time it comes due; the template itself is never executed. "
        "The expression is evaluated in `timezone`, so a schedule stays correct "
        "across daylight-saving transitions."
    ),
)
async def create_schedule(
    queue_id: QueueId, payload: ScheduleCreate, db: DbSession, user: CurrentUser
) -> ScheduleResponse:
    queue = await _authorized_queue(db, user, queue_id)
    schedule = await schedule_service.create_schedule(db, queue, payload)
    return ScheduleResponse.model_validate(schedule)


@router.get(
    "/projects/{project_id}/schedules",
    response_model=list[ScheduleResponse],
    responses=_NOT_FOUND,
    summary="List schedules, soonest first",
)
async def list_schedules(
    project: AccessibleProject,
    db: DbSession,
    queue_id: Annotated[uuid.UUID | None, Query()] = None,
    active_only: Annotated[bool, Query()] = False,
) -> list[ScheduleResponse]:
    rows = await schedule_service.list_schedules(db, project.id, queue_id, active_only)
    return [ScheduleResponse.model_validate(r) for r in rows]


@router.get(
    "/projects/{project_id}/schedules/{schedule_id}",
    response_model=ScheduleResponse,
    responses=_NOT_FOUND,
    summary="Get one schedule",
)
async def get_schedule(
    project: AccessibleProject, schedule_id: ScheduleId, db: DbSession
) -> ScheduleResponse:
    schedule = await schedule_service.get_schedule(db, project.id, schedule_id)
    return ScheduleResponse.model_validate(schedule)


@router.patch(
    "/projects/{project_id}/schedules/{schedule_id}",
    response_model=ScheduleResponse,
    responses=_NOT_FOUND,
    summary="Update a schedule",
    description=(
        "Changing `cron_expression` or `timezone` recomputes `next_run_at` from "
        "now. Reactivating a paused schedule does the same, so a schedule paused "
        "for a week does not fire the instant it is resumed."
    ),
)
async def update_schedule(
    project: AccessibleProject,
    schedule_id: ScheduleId,
    payload: ScheduleUpdate,
    db: DbSession,
) -> ScheduleResponse:
    schedule = await schedule_service.update_schedule(
        db, project.id, schedule_id, payload
    )
    return ScheduleResponse.model_validate(schedule)


@router.delete(
    "/projects/{project_id}/schedules/{schedule_id}",
    response_model=MessageResponse,
    responses=_NOT_FOUND,
    summary="Delete a schedule (jobs it already created are kept)",
)
async def delete_schedule(
    project: AccessibleProject, schedule_id: ScheduleId, db: DbSession
) -> MessageResponse:
    await schedule_service.delete_schedule(db, project.id, schedule_id)
    return MessageResponse(message="Schedule deleted")


@router.post(
    "/projects/{project_id}/schedules/{schedule_id}/trigger",
    response_model=ScheduleTriggerResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_NOT_FOUND,
    summary="Fire a schedule immediately, without shifting its timetable",
)
async def trigger_schedule(
    project: AccessibleProject, schedule_id: ScheduleId, db: DbSession
) -> ScheduleTriggerResponse:
    job_id, schedule = await schedule_service.trigger_schedule(
        db, project.id, schedule_id
    )
    return ScheduleTriggerResponse(
        schedule_id=schedule.id, job_id=job_id, next_run_at=schedule.next_run_at
    )
