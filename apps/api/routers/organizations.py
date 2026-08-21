"""Organization and membership endpoints.

Every route below the collection level declares a `require_role(...)`
dependency. Because the check runs before the handler body, a route physically
cannot serve a caller who lacks the role -- there is no code path where the
authorization check is forgotten.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

from apps.api.core.deps import CurrentUser, DbSession, get_membership, require_role
from apps.api.schemas.common import ErrorResponse, MessageResponse
from apps.api.schemas.organization import (
    MemberAddRequest,
    MemberResponse,
    MemberUpdateRequest,
    OrganizationCreate,
    OrganizationDetailResponse,
    OrganizationResponse,
    OrganizationUpdate,
)
from apps.api.services import org_service
from packages.db import OrgRole

router = APIRouter(prefix="/orgs", tags=["organizations"])

OrgId = Annotated[uuid.UUID, Path(description="Organization id")]
UserId = Annotated[uuid.UUID, Path(description="User id")]

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}
_FORBIDDEN = {403: {"model": ErrorResponse, "description": "Insufficient role"}}


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an organization (caller becomes owner)",
)
async def create_org(
    payload: OrganizationCreate, user: CurrentUser, db: DbSession
) -> OrganizationResponse:
    org = await org_service.create(db, user.id, payload)
    return OrganizationResponse.model_validate(org)


@router.get(
    "",
    response_model=list[OrganizationResponse],
    summary="List organizations the caller belongs to",
)
async def list_orgs(user: CurrentUser, db: DbSession) -> list[OrganizationResponse]:
    orgs = await org_service.list_for_user(db, user.id)
    return [OrganizationResponse.model_validate(o) for o in orgs]


@router.get(
    "/{org_id}",
    response_model=OrganizationDetailResponse,
    responses=_NOT_FOUND,
    summary="Organization detail, including the caller's own role",
)
async def get_org(
    org_id: OrgId, user: CurrentUser, db: DbSession
) -> OrganizationDetailResponse:
    # Any member may read. get_membership raises 404 (not 403) for
    # non-members, so this endpoint cannot be used to probe which
    # organization ids exist.
    membership = await get_membership(db, user.id, org_id)
    detail = await org_service.get_detail(db, org_id, membership.role)
    return OrganizationDetailResponse(**detail)


@router.patch(
    "/{org_id}",
    response_model=OrganizationResponse,
    dependencies=[Depends(require_role(OrgRole.ADMIN))],
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Rename an organization (admin+)",
)
async def update_org(
    org_id: OrgId, payload: OrganizationUpdate, db: DbSession
) -> OrganizationResponse:
    org = await org_service.update(db, org_id, payload)
    return OrganizationResponse.model_validate(org)


@router.delete(
    "/{org_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role(OrgRole.OWNER))],
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Delete an organization and everything in it (owner only)",
)
async def delete_org(org_id: OrgId, db: DbSession) -> None:
    await org_service.delete_org(db, org_id)


# =============================================================================
# Members
# =============================================================================


@router.get(
    "/{org_id}/members",
    response_model=list[MemberResponse],
    responses=_NOT_FOUND,
    summary="List members",
)
async def list_members(
    org_id: OrgId, user: CurrentUser, db: DbSession
) -> list[MemberResponse]:
    await get_membership(db, user.id, org_id)
    return [MemberResponse(**m) for m in await org_service.list_members(db, org_id)]


@router.post(
    "/{org_id}/members",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(OrgRole.ADMIN))],
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Add an existing user to the organization (admin+)",
)
async def add_member(
    org_id: OrgId, payload: MemberAddRequest, db: DbSession
) -> MemberResponse:
    return MemberResponse(**await org_service.add_member(db, org_id, payload))


@router.patch(
    "/{org_id}/members/{user_id}",
    response_model=MessageResponse,
    dependencies=[Depends(require_role(OrgRole.ADMIN))],
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Change a member's role (admin+)",
)
async def update_member(
    org_id: OrgId, user_id: UserId, payload: MemberUpdateRequest, db: DbSession
) -> MessageResponse:
    await org_service.update_member_role(db, org_id, user_id, payload.role)
    return MessageResponse(message=f"Role updated to {payload.role.value}")


@router.delete(
    "/{org_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role(OrgRole.ADMIN))],
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Remove a member (admin+)",
)
async def remove_member(org_id: OrgId, user_id: UserId, db: DbSession) -> None:
    await org_service.remove_member(db, org_id, user_id)
