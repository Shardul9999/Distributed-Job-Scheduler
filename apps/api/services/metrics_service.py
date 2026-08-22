"""Metrics aggregation for the dashboard.

Read-only. Three concerns: how much work is flowing (throughput), how fast it
completes (latency), and whether the system is healthy right now (health).

Time-bucketing uses PostgreSQL's `date_bin`, which snaps every row to a fixed
grid anchored at the Unix epoch. Anchoring matters: without a fixed origin, two
requests a few seconds apart would return buckets offset from each other and the
chart would appear to jitter on every refresh. The buckets are then gap-filled
in Python so an idle minute is a zero, not a missing point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.schemas.metrics import (
    HealthResponse,
    LatencyPoint,
    LatencyResponse,
    ThroughputPoint,
    ThroughputResponse,
)
from packages.db import Job, JobExecution
from packages.db.enums import ExecutionStatus, JobStatus

#: date_bin's grid origin. Any fixed timestamp works; the epoch is the
#: conventional choice and keeps bucket boundaries stable across processes.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _bucket_grid(since: datetime, now: datetime, bucket_seconds: int) -> list[datetime]:
    """The full list of bucket start times date_bin will produce for the window.

    Built so the service can left-join the sparse query result onto a dense grid
    and emit zeros for empty buckets. The first bucket is `since` snapped down to
    the grid, matching how date_bin rounds each row.
    """
    step = timedelta(seconds=bucket_seconds)
    # Snap `since` down to the grid the same way date_bin does.
    elapsed = (since - _EPOCH).total_seconds()
    start = _EPOCH + timedelta(
        seconds=(elapsed // bucket_seconds) * bucket_seconds
    )
    grid: list[datetime] = []
    t = start
    while t <= now:
        grid.append(t)
        t += step
    return grid


async def throughput(
    db: AsyncSession, window_seconds: int, bucket_seconds: int
) -> ThroughputResponse:
    """Executions per bucket, split by outcome.

    Served by `idx_exec_metrics` on (started_at DESC, status): the WHERE clause
    range-scans recent rows and the GROUP BY reads the status off the same index.
    """
    now = datetime.now(UTC)
    since = now - timedelta(seconds=window_seconds)

    stmt = (
        select(
            func.date_bin(
                text(f"interval '{bucket_seconds} seconds'"),
                JobExecution.started_at,
                _EPOCH,
            ).label("bucket"),
            JobExecution.status,
            func.count().label("n"),
        )
        .where(JobExecution.started_at >= since)
        .group_by(text("bucket"), JobExecution.status)
    )
    rows = (await db.execute(stmt)).all()

    # Sparse (bucket, status) -> count, then fold onto the dense grid.
    by_bucket: dict[datetime, ThroughputPoint] = {
        b: ThroughputPoint(bucket=b) for b in _bucket_grid(since, now, bucket_seconds)
    }
    for row in rows:
        point = by_bucket.get(row.bucket)
        if point is None:
            # A row landed just outside the grid we generated; add it rather
            # than dropping data.
            point = ThroughputPoint(bucket=row.bucket)
            by_bucket[row.bucket] = point
        match row.status:
            case ExecutionStatus.SUCCEEDED:
                point.succeeded = row.n
            case ExecutionStatus.FAILED:
                point.failed = row.n
            case ExecutionStatus.TIMEOUT:
                point.timeout = row.n
            case ExecutionStatus.LOST:
                point.lost = row.n

    return ThroughputResponse(
        window_seconds=window_seconds,
        bucket_seconds=bucket_seconds,
        points=[by_bucket[b] for b in sorted(by_bucket)],
    )


async def latency(
    db: AsyncSession, window_seconds: int, bucket_seconds: int
) -> LatencyResponse:
    """Latency percentiles over the window, plus a bucketed p50/p95 line.

    Percentiles come from `percentile_cont`, computed over succeeded executions
    only: a crashed or timed-out attempt's duration is not a measure of useful
    throughput and would drag the distribution.
    """
    now = datetime.now(UTC)
    since = now - timedelta(seconds=window_seconds)

    base = (JobExecution.started_at >= since) & (
        JobExecution.status == ExecutionStatus.SUCCEEDED
    ) & (JobExecution.duration_ms.is_not(None))

    summary = (
        await db.execute(
            select(
                func.percentile_cont(0.5)
                .within_group(JobExecution.duration_ms)
                .label("p50"),
                func.percentile_cont(0.95)
                .within_group(JobExecution.duration_ms)
                .label("p95"),
                func.percentile_cont(0.99)
                .within_group(JobExecution.duration_ms)
                .label("p99"),
                func.avg(JobExecution.duration_ms).label("avg"),
                func.max(JobExecution.duration_ms).label("max"),
                func.count().label("n"),
            ).where(base)
        )
    ).one()

    bucket_stmt = (
        select(
            func.date_bin(
                text(f"interval '{bucket_seconds} seconds'"),
                JobExecution.started_at,
                _EPOCH,
            ).label("bucket"),
            func.percentile_cont(0.5)
            .within_group(JobExecution.duration_ms)
            .label("p50"),
            func.percentile_cont(0.95)
            .within_group(JobExecution.duration_ms)
            .label("p95"),
        )
        .where(base)
        .group_by(text("bucket"))
    )
    bucket_rows = (await db.execute(bucket_stmt)).all()
    seen = {
        row.bucket: LatencyPoint(
            bucket=row.bucket,
            p50_ms=float(row.p50) if row.p50 is not None else None,
            p95_ms=float(row.p95) if row.p95 is not None else None,
        )
        for row in bucket_rows
    }
    points = [
        seen.get(b, LatencyPoint(bucket=b))
        for b in _bucket_grid(since, now, bucket_seconds)
    ]

    return LatencyResponse(
        window_seconds=window_seconds,
        bucket_seconds=bucket_seconds,
        sample_count=summary.n or 0,
        p50_ms=float(summary.p50) if summary.p50 is not None else None,
        p95_ms=float(summary.p95) if summary.p95 is not None else None,
        p99_ms=float(summary.p99) if summary.p99 is not None else None,
        avg_ms=round(float(summary.avg), 1) if summary.avg is not None else None,
        max_ms=int(summary.max) if summary.max is not None else None,
        points=points,
    )


async def health(db: AsyncSession, window_seconds: int) -> HealthResponse:
    """Success/failure over the recent window, plus queue-depth by job status."""
    now = datetime.now(UTC)
    since = now - timedelta(seconds=window_seconds)

    exec_rows = (
        await db.execute(
            select(JobExecution.status, func.count().label("n"))
            .where(JobExecution.started_at >= since)
            .group_by(JobExecution.status)
        )
    ).all()
    by_outcome = {row.status: row.n for row in exec_rows}
    succeeded = by_outcome.get(ExecutionStatus.SUCCEEDED, 0)
    failed = by_outcome.get(ExecutionStatus.FAILED, 0)
    timeout = by_outcome.get(ExecutionStatus.TIMEOUT, 0)
    lost = by_outcome.get(ExecutionStatus.LOST, 0)
    total = succeeded + failed + timeout + lost

    job_rows = (
        await db.execute(
            select(Job.status, func.count().label("n")).group_by(Job.status)
        )
    ).all()
    counts = {row.status: row.n for row in job_rows}
    # Every enum value present, zeros included, so the chart's bars are stable.
    jobs_by_status = {s.value: counts.get(s, 0) for s in JobStatus}

    return HealthResponse(
        window_seconds=window_seconds,
        executions_total=total,
        executions_succeeded=succeeded,
        executions_failed=failed,
        executions_timeout=timeout,
        executions_lost=lost,
        success_rate=(succeeded / total) if total else None,
        failure_rate=((failed + timeout + lost) / total) if total else None,
        jobs_by_status=jobs_by_status,
    )
