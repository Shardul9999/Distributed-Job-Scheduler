"""Queue and retry-policy endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from apps.api.core.deps import (
    AccessibleProject,
    AdminQueue,
    DbSession,
    ReadableQueue,
    WritablePolicy,
    WritableProject,
    WritableQueue,
)
from apps.api.schemas.common import ErrorResponse
from apps.api.schemas.queue import (
    QueueCreate,
    QueueResponse,
    QueueStatsResponse,
    QueueUpdate,
    RetryPolicyCreate,
    RetryPolicyResponse,
    RetryPolicyUpdate,
)
from apps.api.services import queue_service

router = APIRouter(tags=["queues"])

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}
_FORBIDDEN = {403: {"model": ErrorResponse, "description": "Insufficient role"}}


# =============================================================================
# Retry policies
# =============================================================================


@router.post(
    "/projects/{project_id}/retry-policies",
    response_model=RetryPolicyResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Create a reusable retry policy (member+)",
)
async def create_policy(
    project: WritableProject, payload: RetryPolicyCreate, db: DbSession
) -> RetryPolicyResponse:
    policy = await queue_service.create_policy(db, project.id, payload)
    return RetryPolicyResponse.model_validate(policy)


@router.get(
    "/projects/{project_id}/retry-policies",
    response_model=list[RetryPolicyResponse],
    responses=_NOT_FOUND,
    summary="List retry policies",
)
async def list_policies(
    project: AccessibleProject, db: DbSession
) -> list[RetryPolicyResponse]:
    policies = await queue_service.list_policies(db, project.id)
    return [RetryPolicyResponse.model_validate(p) for p in policies]


@router.patch(
    "/retry-policies/{policy_id}",
    response_model=RetryPolicyResponse,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Update a retry policy (member+)",
)
async def update_policy(
    policy: WritablePolicy, payload: RetryPolicyUpdate, db: DbSession
) -> RetryPolicyResponse:
    policy = await queue_service.update_policy(db, policy.id, payload)
    return RetryPolicyResponse.model_validate(policy)


@router.delete(
    "/retry-policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_NOT_FOUND,
        409: {"model": ErrorResponse, "description": "Still referenced by a queue"},
        **_FORBIDDEN,
    },
    summary="Delete a retry policy (member+; 409 if a queue still uses it)",
)
async def delete_policy(policy: WritablePolicy, db: DbSession) -> None:
    await queue_service.delete_policy(db, policy.id)


# =============================================================================
# Queues
# =============================================================================


@router.post(
    "/projects/{project_id}/queues",
    response_model=QueueResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Create a queue (member+)",
)
async def create_queue(
    project: WritableProject, payload: QueueCreate, db: DbSession
) -> QueueResponse:
    queue = await queue_service.create_queue(db, project.id, payload)
    return QueueResponse.model_validate(queue)


@router.get(
    "/projects/{project_id}/queues",
    response_model=list[QueueResponse],
    responses=_NOT_FOUND,
    summary="List a project's queues",
)
async def list_queues(project: AccessibleProject, db: DbSession) -> list[QueueResponse]:
    queues = await queue_service.list_queues(db, project.id)
    return [QueueResponse.model_validate(q) for q in queues]


@router.get(
    "/queues/{queue_id}",
    response_model=QueueResponse,
    responses=_NOT_FOUND,
    summary="Fetch a queue",
)
async def get_queue(queue: ReadableQueue) -> QueueResponse:
    return QueueResponse.model_validate(queue)


@router.patch(
    "/queues/{queue_id}",
    response_model=QueueResponse,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Update queue configuration (member+)",
)
async def update_queue(
    queue: WritableQueue, payload: QueueUpdate, db: DbSession
) -> QueueResponse:
    updated = await queue_service.update_queue(db, queue.id, payload)
    return QueueResponse.model_validate(updated)


@router.post(
    "/queues/{queue_id}/pause",
    response_model=QueueResponse,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Pause a queue (member+; running jobs finish, no new claims)",
)
async def pause_queue(queue: WritableQueue, db: DbSession) -> QueueResponse:
    return QueueResponse.model_validate(
        await queue_service.set_paused(db, queue.id, True)
    )


@router.post(
    "/queues/{queue_id}/resume",
    response_model=QueueResponse,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Resume a paused queue (member+)",
)
async def resume_queue(queue: WritableQueue, db: DbSession) -> QueueResponse:
    return QueueResponse.model_validate(
        await queue_service.set_paused(db, queue.id, False)
    )


@router.get(
    "/queues/{queue_id}/stats",
    response_model=QueueStatsResponse,
    responses=_NOT_FOUND,
    summary="Queue depth, throughput and health",
)
async def queue_stats(queue: ReadableQueue, db: DbSession) -> QueueStatsResponse:
    return QueueStatsResponse(**await queue_service.get_stats(db, queue.id))


@router.delete(
    "/queues/{queue_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Delete a queue and all its jobs (admin+)",
)
async def delete_queue(queue: AdminQueue, db: DbSession) -> None:
    await queue_service.delete_queue(db, queue.id)
