"""Metrics contracts for the dashboard's charts.

These three endpoints are read-only aggregates over `job_executions` and `jobs`.
They are global rather than project-scoped, like the fleet view: the overview
page answers "is the whole system healthy", and every number below is derived
from an index the database already carries (`idx_exec_metrics` for the
time-bucketed scans, the status columns for the depth breakdowns).

Time series are returned *gap-filled*: the service emits one point per bucket
across the whole window, zeros included, so the frontend charts a continuous
line without having to reconstruct missing intervals itself.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ThroughputPoint(BaseModel):
    """One time bucket, executions split by outcome.

    The four outcomes are the `execution_status` enum: a `succeeded`/`failed`
    split is the failure-rate chart, and `lost` surfaces crash-recovery volume
    separately so an infrastructure problem is not mistaken for a code bug.
    """

    bucket: datetime
    succeeded: int = 0
    failed: int = 0
    timeout: int = 0
    lost: int = 0

    @property
    def total(self) -> int:
        return self.succeeded + self.failed + self.timeout + self.lost


class ThroughputResponse(BaseModel):
    window_seconds: int
    bucket_seconds: int
    points: list[ThroughputPoint]


class LatencyPoint(BaseModel):
    bucket: datetime
    p50_ms: float | None = None
    p95_ms: float | None = None


class LatencyResponse(BaseModel):
    """Percentiles over the window, plus a bucketed p50/p95 line.

    Percentiles, not an average: a mean latency hides the tail, and the tail is
    what a job queue is judged on. Computed over `succeeded` executions only --
    a failed attempt's duration measures how fast it broke, not how fast the
    system does useful work.
    """

    window_seconds: int
    bucket_seconds: int
    sample_count: int
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    avg_ms: float | None = None
    max_ms: int | None = None
    points: list[LatencyPoint]


class HealthResponse(BaseModel):
    """Instantaneous system health for the overview page's headline tiles.

    `jobs_by_status` is the queue-depth breakdown that drives the depth chart;
    the execution counts and `success_rate` over the recent window drive the
    failure-rate tile.
    """

    window_seconds: int

    executions_total: int = 0
    executions_succeeded: int = 0
    executions_failed: int = 0
    executions_timeout: int = 0
    executions_lost: int = 0

    #: succeeded / total over the window, or None when nothing has run yet.
    success_rate: float | None = None
    #: (failed + timeout + lost) / total over the window.
    failure_rate: float | None = None

    #: Every job status -> count, for the queue-depth chart. Includes zero
    #: entries so the chart's categories are stable across refreshes.
    jobs_by_status: dict[str, int]
