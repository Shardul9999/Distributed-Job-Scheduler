"""Shared FastAPI dependencies: database sessions, authentication, RBAC.

Authorization lives here rather than inside each route handler. A route declares
what it needs (`user: CurrentUser`, `_: Annotated[None, Depends(require_role(...))]`)
and cannot accidentally forget the check -- the dependency runs before the
handler body does.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Path
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.errors import AuthenticationError, AuthorizationError, NotFoundError
from apps.api.core.security import TokenError, decode_token
from packages.db import (
    Organization,
    OrganizationMember,
    OrgRole,
    Project,
    Queue,
    RetryPolicy,
    User,
    get_session,
)

# auto_error=False so a missing header raises our own AuthenticationError
# (rendered in the standard envelope) rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Resolve the bearer token to a live, active user.

    The user row is re-read on every request rather than trusted from token
    claims. It costs one indexed primary-key lookup and means deactivating an
    account takes effect immediately instead of whenever the token expires.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing bearer token")

    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    try:
        user_id = uuid.UUID(payload["sub"])
    except (ValueError, KeyError) as exc:
        raise AuthenticationError("Token subject is not a valid user id") from exc

    user = await db.get(User, user_id)
    if user is None:
        raise AuthenticationError("User no longer exists")
    if not user.is_active:
        raise AuthenticationError("User account is disabled")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# =============================================================================
# Organization access
# =============================================================================

#: Privilege ordering. A check for MEMBER is satisfied by ADMIN and OWNER.
#: Ranking roles rather than comparing them for equality is what keeps
#: authorization checks from turning into long membership tests.
_ROLE_RANK: dict[OrgRole, int] = {
    OrgRole.VIEWER: 0,
    OrgRole.MEMBER: 1,
    OrgRole.ADMIN: 2,
    OrgRole.OWNER: 3,
}


async def get_membership(
    db: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID
) -> OrganizationMember:
    """Load the caller's membership, or refuse.

    Note this raises NotFound, not Forbidden, when the caller is not a member.
    Returning 403 would confirm that an organization with this id exists --
    a membership-enumeration leak. Non-members get the same answer as they
    would for an id that does not exist at all.
    """
    membership = await db.get(OrganizationMember, (org_id, user_id))
    if membership is None:
        raise NotFoundError("Organization not found")
    return membership


def _assert_rank(actual: OrgRole, minimum: OrgRole) -> None:
    """Refuse a caller whose role sits below `minimum` in the ranking."""
    if _ROLE_RANK[actual] < _ROLE_RANK[minimum]:
        raise AuthorizationError(
            f"This action requires the {minimum.value} role or higher; "
            f"you have {actual.value}"
        )


def require_role(minimum: OrgRole) -> Callable:
    """Build a dependency enforcing a minimum role on the path's organization.

    Usage:
        @router.delete(
            "/orgs/{org_id}",
            dependencies=[Depends(require_role(OrgRole.OWNER))],
        )
    """

    async def _dependency(
        db: DbSession,
        user: CurrentUser,
        org_id: Annotated[uuid.UUID, Path()],
    ) -> OrganizationMember:
        membership = await get_membership(db, user.id, org_id)
        _assert_rank(membership.role, minimum)
        return membership

    return _dependency


# =============================================================================
# Project access
# =============================================================================


async def get_accessible_project(
    db: DbSession,
    user: CurrentUser,
    project_id: Annotated[uuid.UUID, Path()],
) -> Project:
    """Load a project, verifying the caller belongs to its organization.

    One join rather than two round trips: fetching the project and then
    checking membership separately would be two queries and would briefly hold
    a project the caller may not be entitled to see.
    """
    stmt = (
        select(Project)
        .join(Organization, Organization.id == Project.org_id)
        .join(
            OrganizationMember,
            (OrganizationMember.org_id == Organization.id)
            & (OrganizationMember.user_id == user.id),
        )
        .where(Project.id == project_id)
    )
    project = (await db.execute(stmt)).scalar_one_or_none()
    if project is None:
        raise NotFoundError("Project not found")
    return project


AccessibleProject = Annotated[Project, Depends(get_accessible_project)]


# =============================================================================
# Role enforcement inside a project
# =============================================================================
#
# `require_role` above answers "what may you do to this organization?" and reads
# `org_id` from the path. Everything below a project is addressed by a different
# id -- `project_id`, `queue_id`, `policy_id` -- so it needs its own resolvers.
#
# Each one proves tenancy and reads the caller's role in a *single* query: the
# join chain resource -> project -> org -> membership already has to run to
# establish access, so selecting the membership row alongside makes the role
# check free rather than a second round trip.


async def _resolve_scoped(
    db: AsyncSession,
    user_id: uuid.UUID,
    model: type,
    resource_id: uuid.UUID,
    label: str,
) -> tuple[object, OrganizationMember]:
    """Load a project-owned resource together with the caller's membership.

    `model` is `Project` itself, or anything carrying a `project_id` column
    (`Queue`, `RetryPolicy`). Missing and forbidden collapse to the same
    NotFoundError for the same reason `get_membership` does: a 403 would confirm
    the id exists and let a caller enumerate other tenants' resources.
    """
    stmt = select(model, OrganizationMember)
    if model is not Project:
        stmt = stmt.join(Project, Project.id == model.project_id)
    stmt = (
        stmt.join(Organization, Organization.id == Project.org_id)
        .join(
            OrganizationMember,
            (OrganizationMember.org_id == Organization.id)
            & (OrganizationMember.user_id == user_id),
        )
        .where(model.id == resource_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise NotFoundError(f"{label} not found")
    return row[0], row[1]


def require_project_role(minimum: OrgRole) -> Callable:
    """Dependency: minimum role on the organization owning the path's project."""

    async def _dependency(
        db: DbSession,
        user: CurrentUser,
        project_id: Annotated[uuid.UUID, Path()],
    ) -> Project:
        project, membership = await _resolve_scoped(
            db, user.id, Project, project_id, "Project"
        )
        _assert_rank(membership.role, minimum)
        return project  # type: ignore[return-value]

    return _dependency


def require_queue_role(minimum: OrgRole) -> Callable:
    """Dependency: minimum role on the organization owning the path's queue."""

    async def _dependency(
        db: DbSession,
        user: CurrentUser,
        queue_id: Annotated[uuid.UUID, Path()],
    ) -> Queue:
        queue, membership = await _resolve_scoped(
            db, user.id, Queue, queue_id, "Queue"
        )
        _assert_rank(membership.role, minimum)
        return queue  # type: ignore[return-value]

    return _dependency


def require_policy_role(minimum: OrgRole) -> Callable:
    """Dependency: minimum role on the organization owning the path's policy."""

    async def _dependency(
        db: DbSession,
        user: CurrentUser,
        policy_id: Annotated[uuid.UUID, Path()],
    ) -> RetryPolicy:
        policy, membership = await _resolve_scoped(
            db, user.id, RetryPolicy, policy_id, "Retry policy"
        )
        _assert_rank(membership.role, minimum)
        return policy  # type: ignore[return-value]

    return _dependency


# Named aliases, so a route's signature states the privilege it demands and the
# rule lives in one place rather than being re-typed at each call site.
#
# MEMBER is the line between watching the system and changing it: a VIEWER may
# read every page of the dashboard and alter nothing. ADMIN is reserved for the
# operations that destroy history or issue credentials.
WritableProject = Annotated[Project, Depends(require_project_role(OrgRole.MEMBER))]
AdminProject = Annotated[Project, Depends(require_project_role(OrgRole.ADMIN))]

WritableQueue = Annotated[Queue, Depends(require_queue_role(OrgRole.MEMBER))]
AdminQueue = Annotated[Queue, Depends(require_queue_role(OrgRole.ADMIN))]

ReadableQueue = Annotated[Queue, Depends(require_queue_role(OrgRole.VIEWER))]
WritablePolicy = Annotated[RetryPolicy, Depends(require_policy_role(OrgRole.MEMBER))]
