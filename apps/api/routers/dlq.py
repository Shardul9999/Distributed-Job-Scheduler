"""Dead-letter queue endpoints: inspect, replay, discard."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from apps.api.core.deps import AccessibleProject, DbSession
from apps.api.core.pagination import PageParams, page_params
from apps.api.schemas.common import ErrorResponse, MessageResponse, PageResponse
from apps.api.schemas.dlq import DeadLetterResponse, ReplayRequest, ReplayResponse
from apps.api.services import dlq_service

router = APIRouter(tags=["dead letter queue"])

EntryId = Annotated[uuid.UUID, Path(description="Dead letter entry id")]

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}


@router.get(
    "/projects/{project_id}/dlq",
    response_model=PageResponse[DeadLetterResponse],
    responses=_NOT_FOUND,
    summary="List dead-lettered jobs, most recent death first",
    description=(
        "Keyset paginated on `died_at`. Pass `unreplayed_only=true` for the "
        "operator's working set -- failures nobody has dealt with yet."
    ),
)
async def list_dlq(
    project: AccessibleProject,
    db: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    queue_id: Annotated[uuid.UUID | None, Query()] = None,
    unreplayed_only: Annotated[bool, Query()] = False,
) -> PageResponse[DeadLetterResponse]:
    rows, next_cursor, has_more = await dlq_service.list_entries(
        db, project.id, params, queue_id, unreplayed_only
    )
    return PageResponse[DeadLetterResponse](
        items=[DeadLetterResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
        has_more=has_more,
        limit=params.limit,
    )


@router.get(
    "/projects/{project_id}/dlq/{entry_id}",
    response_model=DeadLetterResponse,
    responses=_NOT_FOUND,
    summary="Inspect one dead-lettered job, with its final error and stack",
)
async def get_dlq_entry(
    project: AccessibleProject, entry_id: EntryId, db: DbSession
) -> DeadLetterResponse:
    entry = await dlq_service.get_entry(db, project.id, entry_id)
    return DeadLetterResponse.model_validate(entry)


@router.post(
    "/projects/{project_id}/dlq/{entry_id}/replay",
    response_model=ReplayResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_NOT_FOUND,
    summary="Re-enqueue a dead job as a new job",
    description=(
        "Creates a **new** job and stamps this entry with `replayed_job_id`. "
        "The original stays `dead` and the entry stays in place, so the record "
        "of what failed survives the fix. Optionally amend the payload, attempt "
        "budget, priority, or target queue as part of the replay. An entry can "
        "only be replayed once."
    ),
)
async def replay_entry(
    project: AccessibleProject,
    entry_id: EntryId,
    payload: ReplayRequest,
    db: DbSession,
) -> ReplayResponse:
    entry, job = await dlq_service.replay(db, project.id, entry_id, payload)
    return ReplayResponse(
        dlq_entry_id=entry.id,
        original_job_id=entry.job_id,
        replayed_job_id=job.id,
        queue_id=job.queue_id,
    )


@router.delete(
    "/projects/{project_id}/dlq/{entry_id}",
    response_model=MessageResponse,
    responses=_NOT_FOUND,
    summary="Discard a dead-letter entry (the dead job row is kept)",
)
async def discard_entry(
    project: AccessibleProject, entry_id: EntryId, db: DbSession
) -> MessageResponse:
    await dlq_service.discard(db, project.id, entry_id)
    return MessageResponse(message="Dead letter entry discarded")
