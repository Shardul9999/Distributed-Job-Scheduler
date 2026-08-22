"""Worker fleet contracts.

Workers are infrastructure, not tenant data: a worker process registers itself
on boot and polls whichever queues it was configured with, across projects. The
fleet view is therefore operator-facing and scoped to an authenticated user
rather than to a project -- and says so, rather than pretending to a tenancy
model it does not have.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, computed_field

from apps.api.schemas.common import ORMModel
from packages.db.enums import WorkerStatus


class WorkerResponse(ORMModel):
    id: uuid.UUID
    hostname: str
    pid: int
    version: str
    concurrency: int
    queue_names: list[str] | None
    status: WorkerStatus
    jobs_processed: int
    started_at: datetime
    last_heartbeat_at: datetime
    stopped_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def heartbeat_age_s(self) -> float:
        """Seconds since this worker last proved it was alive.

        Computed rather than stored: the dashboard needs "is this worker
        healthy *right now*", and a value written at heartbeat time would be
        stale by definition.
        """
        beat = self.last_heartbeat_at
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=UTC)
        return (datetime.now(UTC) - beat).total_seconds()


class WorkerHeartbeatResponse(ORMModel):
    beat_at: datetime
    active_jobs: int
    jobs_processed: int
    cpu_percent: int | None
    memory_mb: int | None


class WorkerDetailResponse(WorkerResponse):
    recent_heartbeats: list[WorkerHeartbeatResponse] = []


class FleetStatsResponse(BaseModel):
    """Whole-system health, for the dashboard's overview page."""

    workers_total: int = 0
    workers_active: int = 0
    workers_draining: int = 0
    workers_dead: int = 0
    workers_stopped: int = 0

    #: Sum of `concurrency` across live workers: the fleet's theoretical
    #: parallelism ceiling, against which `jobs_in_flight` is the utilisation.
    fleet_capacity: int = 0
    jobs_in_flight: int = 0

    jobs_backlog: int = 0
    jobs_dead: int = 0
    dlq_unreplayed: int = 0
    schedules_active: int = 0

    #: Whether some replica currently holds the scheduler's advisory lock.
    #: False means cron is not firing and orphans are not being recovered --
    #: the single most important thing to surface about this system's health,
    #: and invisible from job counts alone.
    scheduler_leader_present: bool = False
