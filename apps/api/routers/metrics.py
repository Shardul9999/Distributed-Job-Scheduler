"""Metrics endpoints: throughput, latency, health.

Global and read-only, like the fleet view -- the dashboard's overview page is a
whole-system picture, and every figure here is a database-side aggregate rather
than anything the API computes row by row. Windows and bucket sizes are
client-chosen but bounded, so a caller cannot ask for a million one-second
buckets and turn a chart request into a table scan.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from apps.api.core.deps import CurrentUser, DbSession
from apps.api.schemas.metrics import (
    HealthResponse,
    LatencyResponse,
    ThroughputResponse,
)
from apps.api.services import metrics_service

router = APIRouter(prefix="/metrics", tags=["metrics"])

# Bounds shared by the time-series endpoints. Five minutes to 24 hours of window;
# one second to one hour per bucket. The upper window / lower bucket combination
# is what keeps the point count sane.
WindowSeconds = Annotated[int, Query(ge=300, le=86_400)]
BucketSeconds = Annotated[int, Query(ge=1, le=3_600)]


@router.get(
    "/throughput",
    response_model=ThroughputResponse,
    summary="Executions per interval, split by outcome",
    description=(
        "Time-bucketed execution counts (succeeded / failed / timeout / lost) "
        "over the window. Gap-filled: an idle bucket is a zero, so the chart "
        "line stays continuous. Drives the overview throughput and failure-rate "
        "charts."
    ),
)
async def get_throughput(
    db: DbSession,
    user: CurrentUser,
    window_seconds: WindowSeconds = 3_600,
    bucket_seconds: BucketSeconds = 60,
) -> ThroughputResponse:
    return await metrics_service.throughput(db, window_seconds, bucket_seconds)


@router.get(
    "/latency",
    response_model=LatencyResponse,
    summary="Execution-duration percentiles",
    description=(
        "p50 / p95 / p99 duration over succeeded executions in the window, plus "
        "a bucketed p50/p95 line. Percentiles rather than an average: the tail "
        "is what a queue is judged on."
    ),
)
async def get_latency(
    db: DbSession,
    user: CurrentUser,
    window_seconds: WindowSeconds = 3_600,
    bucket_seconds: BucketSeconds = 60,
) -> LatencyResponse:
    return await metrics_service.latency(db, window_seconds, bucket_seconds)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Instantaneous system health",
    description=(
        "Success and failure rates over the recent window, plus a queue-depth "
        "breakdown of every job status. The headline tiles on the overview page."
    ),
)
async def get_health(
    db: DbSession,
    user: CurrentUser,
    window_seconds: WindowSeconds = 3_600,
) -> HealthResponse:
    return await metrics_service.health(db, window_seconds)
