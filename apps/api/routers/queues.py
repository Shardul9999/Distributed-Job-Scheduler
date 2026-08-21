"""Queue and retry-policy endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from apps.api.core.deps import AccessibleProject, CurrentUser, DbSession
from apps.api.core.errors import NotFoundError
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
from packages.db import Organization, OrganizationMember, Project, Queue

router = APIRouter(tags=["queues"])

QueueId = Annotated[uuid.UUID, Path(description="Queue id")]
PolicyId = Annotated[uuid.UUID, Path(description="Retry policy id")]

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}


async def _authorized_queue(db: DbSession, user: CurrentUser, queue_id: uuid.UUID) -> Queue:
    """Load a queue only if the caller belongs to its owning organization.

    Queue ids are exposed throughout the dashboard, so every queue-scoped route
    must re-verify tenancy rather than trusting the id. One join chain
    (queue -> project -> org -> membership) does it in a single query.
    """
    from sqlalchemy import select

    stmt = (
        select(Queue)
        .join(Project, Project.id == Queue.project_id)
        .join(Organization, Organization.id == Project.org_id)
        .join(
            OrganizationMember,
            (OrganizationMember.org_id == Organization.id)
            & (OrganizationMember.user_id == user.id),
        )
        .where(Queue.id == queue_id)
    )
    queue = (await db.execute(stmt)).scalar_one_or_none()
    if queue is None:
        raise NotFoundError("Queue not found")
    return queue


# =============================================================================
# Retry policies
# =============================================================================


@router.post(
    "/projects/{project_id}/retry-policies",
    response_model=RetryPolicyResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_NOT_FOUND,
    summary="Create a reusable retry policy",
)
async def create_policy(
    project: AccessibleProject, payload: RetryPolicyCreate, db: DbSession
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
    responses=_NOT_FOUND,
    summary="Update a retry policy",
)
async def update_policy(
    policy_id: PolicyId, payload: RetryPolicyUpdate, db: DbSession, user: CurrentUser
) -> RetryPolicyResponse:
    policy = await queue_service.update_policy(db, policy_id, payload)
    return RetryPolicyResponse.model_validate(policy)


@router.delete(
    "/retry-policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_NOT_FOUND,
        409: {"model": ErrorResponse, "description": "Still referenced by a queue"},
    },
    summary="Delete a retry policy (409 if a queue still uses it)",
)
async def delete_policy(
    policy_id: PolicyId, db: DbSession, user: CurrentUser
) -> None:
    await queue_service.delete_policy(db, policy_id)


# =============================================================================
# Queues
# =============================================================================


@router.post(
    "/projects/{project_id}/queues",
    response_model=QueueResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_NOT_FOUND,
    summary="Create a queue",
)
async def create_queue(
    project: AccessibleProject, payload: QueueCreate, db: DbSession
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
async def get_queue(
    queue_id: QueueId, db: DbSession, user: CurrentUser
) -> QueueResponse:
    return QueueResponse.model_validate(await _authorized_queue(db, user, queue_id))


@router.patch(
    "/queues/{queue_id}",
    response_model=QueueResponse,
    responses=_NOT_FOUND,
    summary="Update queue configuration",
)
async def update_queue(
    queue_id: QueueId, payload: QueueUpdate, db: DbSession, user: CurrentUser
) -> QueueResponse:
    await _authorized_queue(db, user, queue_id)
    queue = await queue_service.update_queue(db, queue_id, payload)
    return QueueResponse.model_validate(queue)


@router.post(
    "/queues/{queue_id}/pause",
    response_model=QueueResponse,
    responses=_NOT_FOUND,
    summary="Pause a queue (running jobs finish; no new claims)",
)
async def pause_queue(
    queue_id: QueueId, db: DbSession, user: CurrentUser
) -> QueueResponse:
    await _authorized_queue(db, user, queue_id)
    return QueueResponse.model_validate(
        await queue_service.set_paused(db, queue_id, True)
    )


@router.post(
    "/queues/{queue_id}/resume",
    response_model=QueueResponse,
    responses=_NOT_FOUND,
    summary="Resume a paused queue",
)
async def resume_queue(
    queue_id: QueueId, db: DbSession, user: CurrentUser
) -> QueueResponse:
    await _authorized_queue(db, user, queue_id)
    return QueueResponse.model_validate(
        await queue_service.set_paused(db, queue_id, False)
    )


@router.get(
    "/queues/{queue_id}/stats",
    response_model=QueueStatsResponse,
    responses=_NOT_FOUND,
    summary="Queue depth, throughput and health",
)
async def queue_stats(
    queue_id: QueueId, db: DbSession, user: CurrentUser
) -> QueueStatsResponse:
    await _authorized_queue(db, user, queue_id)
    return QueueStatsResponse(**await queue_service.get_stats(db, queue_id))


@router.delete(
    "/queues/{queue_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_NOT_FOUND,
    summary="Delete a queue and all its jobs",
)
async def delete_queue(queue_id: QueueId, db: DbSession, user: CurrentUser) -> None:
    await _authorized_queue(db, user, queue_id)
    await queue_service.delete_queue(db, queue_id)
