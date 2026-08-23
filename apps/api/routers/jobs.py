"""Job endpoints: enqueue, explore, inspect, retry, cancel."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, status

from apps.api.core.deps import (
    AccessibleProject,
    DbSession,
    WritableProject,
    WritableQueue,
)
from apps.api.core.pagination import PageParams, page_params
from apps.api.schemas.common import ErrorResponse, PageResponse
from apps.api.schemas.job import (
    BatchJobCreate,
    BatchJobResponse,
    JobCreate,
    JobDetailResponse,
    JobExecutionResponse,
    JobFilters,
    JobLogResponse,
    JobResponse,
)
from apps.api.services import job_service
from packages.db.enums import JobStatus

from fastapi import Depends

router = APIRouter(tags=["jobs"])

JobId = Annotated[uuid.UUID, Path(description="Job id")]

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}
_FORBIDDEN = {403: {"model": ErrorResponse, "description": "Insufficient role"}}


@router.post(
    "/queues/{queue_id}/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Enqueue a job (member+; immediate, delayed, or scheduled)",
    description=(
        "Omit `run_at` and `delay_seconds` to run immediately. Supply "
        "`delay_seconds` to run after a delay, or `run_at` for a specific "
        "time. Send an `Idempotency-Key` header (or `idempotency_key` in the "
        "body) to make retried submissions safe."
    ),
)
async def create_job(
    queue: WritableQueue,
    payload: JobCreate,
    db: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JobResponse:
    # Header wins over body: it is the conventional transport for this and is
    # what an HTTP client library will set automatically on retry.
    if idempotency_key and not payload.idempotency_key:
        payload.idempotency_key = idempotency_key

    job = await job_service.enqueue(db, queue, payload)
    return JobResponse.model_validate(job)


@router.post(
    "/queues/{queue_id}/jobs/batch",
    response_model=BatchJobResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Enqueue up to 1000 jobs in one transaction (member+)",
)
async def create_batch(
    queue: WritableQueue, payload: BatchJobCreate, db: DbSession
) -> BatchJobResponse:
    batch_id, job_ids = await job_service.enqueue_batch(db, queue, payload)
    return BatchJobResponse(batch_id=batch_id, created=len(job_ids), job_ids=job_ids)


@router.get(
    "/projects/{project_id}/jobs",
    response_model=PageResponse[JobResponse],
    responses=_NOT_FOUND,
    summary="Job explorer: filter and page through jobs",
    description=(
        "Keyset paginated. Pass the `next_cursor` from a response as `cursor` "
        "to fetch the following page; cost is constant at any depth."
    ),
)
async def list_jobs(
    project: AccessibleProject,
    db: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    queue_id: Annotated[uuid.UUID | None, Query()] = None,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    job_type: Annotated[str | None, Query()] = None,
    batch_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PageResponse[JobResponse]:
    filters = JobFilters(
        queue_id=queue_id, status=job_status, job_type=job_type, batch_id=batch_id
    )
    rows, next_cursor, has_more = await job_service.list_jobs(
        db, project.id, filters, params
    )
    return PageResponse[JobResponse](
        items=[JobResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
        has_more=has_more,
        limit=params.limit,
    )


@router.get(
    "/projects/{project_id}/jobs/{job_id}",
    response_model=JobDetailResponse,
    responses=_NOT_FOUND,
    summary="Job detail with full attempt history",
)
async def get_job(
    project: AccessibleProject, job_id: JobId, db: DbSession
) -> JobDetailResponse:
    job = await job_service.get_job(db, project.id, job_id)
    executions = await job_service.list_executions(db, job_id)
    return JobDetailResponse(
        **JobResponse.model_validate(job).model_dump(),
        executions=[JobExecutionResponse.model_validate(e) for e in executions],
    )


@router.get(
    "/projects/{project_id}/jobs/{job_id}/executions",
    response_model=list[JobExecutionResponse],
    responses=_NOT_FOUND,
    summary="Every attempt made on this job",
)
async def job_executions(
    project: AccessibleProject, job_id: JobId, db: DbSession
) -> list[JobExecutionResponse]:
    await job_service.get_job(db, project.id, job_id)
    rows = await job_service.list_executions(db, job_id)
    return [JobExecutionResponse.model_validate(r) for r in rows]


@router.get(
    "/projects/{project_id}/jobs/{job_id}/logs",
    response_model=list[JobLogResponse],
    responses=_NOT_FOUND,
    summary="Log lines emitted during execution",
)
async def job_logs(
    project: AccessibleProject,
    job_id: JobId,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[JobLogResponse]:
    await job_service.get_job(db, project.id, job_id)
    rows = await job_service.list_logs(db, job_id, limit)
    return [JobLogResponse.model_validate(r) for r in rows]


@router.post(
    "/projects/{project_id}/jobs/{job_id}/retry",
    response_model=JobResponse,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Requeue a failed or dead job (member+), resetting its attempt count",
)
async def retry_job(
    project: WritableProject, job_id: JobId, db: DbSession
) -> JobResponse:
    job = await job_service.retry_job(db, project.id, job_id)
    return JobResponse.model_validate(job)


@router.post(
    "/projects/{project_id}/jobs/{job_id}/cancel",
    response_model=JobResponse,
    responses={
        **_NOT_FOUND,
        409: {"model": ErrorResponse, "description": "Already running or terminal"},
        **_FORBIDDEN,
    },
    summary="Cancel a job that has not started (member+)",
)
async def cancel_job(
    project: WritableProject, job_id: JobId, db: DbSession
) -> JobResponse:
    job = await job_service.cancel_job(db, project.id, job_id)
    return JobResponse.model_validate(job)
