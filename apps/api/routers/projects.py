"""Project endpoints.

Routing reflects ownership: collection operations hang off the organization
(`/orgs/{org_id}/projects`) because that is the scope in which a project is
created and listed, while item operations use the flat `/projects/{project_id}`
form because a project id is globally unique and the client should not need to
remember its parent to fetch it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

from apps.api.core.deps import (
    AccessibleProject,
    CurrentUser,
    DbSession,
    get_membership,
    require_role,
)
from apps.api.schemas.common import ErrorResponse
from apps.api.schemas.project import (
    ApiKeyResponse,
    ProjectCreate,
    ProjectDetailResponse,
    ProjectResponse,
    ProjectUpdate,
)
from apps.api.services import project_service
from packages.db import OrgRole

router = APIRouter(tags=["projects"])

OrgId = Annotated[uuid.UUID, Path(description="Organization id")]

_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}
_FORBIDDEN = {403: {"model": ErrorResponse, "description": "Insufficient role"}}


@router.post(
    "/orgs/{org_id}/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(OrgRole.MEMBER))],
    responses={**_NOT_FOUND, **_FORBIDDEN},
    summary="Create a project (member+)",
)
async def create_project(
    org_id: OrgId, payload: ProjectCreate, db: DbSession
) -> ProjectResponse:
    project = await project_service.create(db, org_id, payload)
    return ProjectResponse.model_validate(project)


@router.get(
    "/orgs/{org_id}/projects",
    response_model=list[ProjectDetailResponse],
    responses=_NOT_FOUND,
    summary="List an organization's projects with queue counts",
)
async def list_projects(
    org_id: OrgId, user: CurrentUser, db: DbSession
) -> list[ProjectDetailResponse]:
    await get_membership(db, user.id, org_id)
    rows = await project_service.list_for_org(db, org_id)
    return [ProjectDetailResponse(**r) for r in rows]


@router.get(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    responses=_NOT_FOUND,
    summary="Fetch a project",
)
async def get_project(project: AccessibleProject) -> ProjectResponse:
    # AccessibleProject resolves the id and verifies organization membership in
    # a single joined query -- see core/deps.py.
    return ProjectResponse.model_validate(project)


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    responses=_NOT_FOUND,
    summary="Update a project",
)
async def update_project(
    project: AccessibleProject, payload: ProjectUpdate, db: DbSession
) -> ProjectResponse:
    updated = await project_service.update(db, project.id, payload)
    return ProjectResponse.model_validate(updated)


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_NOT_FOUND,
    summary="Delete a project and all its queues and jobs",
)
async def delete_project(project: AccessibleProject, db: DbSession) -> None:
    await project_service.delete_project(db, project.id)


@router.post(
    "/projects/{project_id}/api-key",
    response_model=ApiKeyResponse,
    responses=_NOT_FOUND,
    summary="Rotate the project API key (plaintext returned once)",
)
async def rotate_api_key(
    project: AccessibleProject, db: DbSession
) -> ApiKeyResponse:
    key = await project_service.rotate_api_key(db, project.id)
    return ApiKeyResponse(api_key=key)
