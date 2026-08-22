"""Worker fleet endpoints: who is running, how hard, and is the scheduler alive.

Not project-scoped. A worker process is infrastructure that polls queues across
projects, so scoping the fleet view to a project would be a fiction. Access
requires authentication; anything finer would have to be invented rather than
derived from the data model.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from apps.api.core.deps import CurrentUser, DbSession
from apps.api.schemas.common import ErrorResponse
from apps.api.schemas.worker import (
    FleetStatsResponse,
    WorkerDetailResponse,
    WorkerHeartbeatResponse,
    WorkerResponse,
)
from apps.api.services import worker_service

router = APIRouter(tags=["fleet"])

WorkerId = Annotated[uuid.UUID, Path(description="Worker id")]

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}


@router.get(
    "/workers",
    response_model=list[WorkerResponse],
    summary="List worker processes",
    description=(
        "Live workers by default. `include_stopped=true` also returns dead and "
        "stopped rows, which is what you want when tracing which worker was "
        "holding a job at the moment it was lost."
    ),
)
async def list_workers(
    db: DbSession,
    user: CurrentUser,
    include_stopped: Annotated[bool, Query()] = False,
) -> list[WorkerResponse]:
    rows = await worker_service.list_workers(db, include_stopped)
    return [WorkerResponse.model_validate(r) for r in rows]


@router.get(
    "/workers/{worker_id}",
    response_model=WorkerDetailResponse,
    responses=_NOT_FOUND,
    summary="Worker detail with recent heartbeat samples",
)
async def get_worker(
    worker_id: WorkerId,
    db: DbSession,
    user: CurrentUser,
    samples: Annotated[int, Query(ge=1, le=500)] = 60,
) -> WorkerDetailResponse:
    worker = await worker_service.get_worker(db, worker_id)
    beats = await worker_service.recent_heartbeats(db, worker_id, samples)
    detail = WorkerDetailResponse.model_validate(worker)
    detail.recent_heartbeats = [
        WorkerHeartbeatResponse.model_validate(b) for b in beats
    ]
    return detail


@router.get(
    "/fleet/stats",
    response_model=FleetStatsResponse,
    summary="Whole-system health for the dashboard overview",
    description=(
        "Fleet size and capacity, job depth by phase, DLQ backlog, and whether "
        "a scheduler currently holds the leader lock. That last field is the "
        "one to alert on: with no leader, cron stops firing and orphaned jobs "
        "stop being recovered, and neither failure is visible in job counts "
        "until it is already a backlog."
    ),
)
async def get_fleet_stats(db: DbSession, user: CurrentUser) -> FleetStatsResponse:
    return await worker_service.fleet_stats(db)
