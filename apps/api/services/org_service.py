"""Organization and membership operations."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from apps.api.schemas.organization import (
    MemberAddRequest,
    OrganizationCreate,
    OrganizationUpdate,
)
from apps.api.services.slugs import unique_slug
from packages.db import Organization, OrganizationMember, OrgRole, Project, User
from packages.db.enums import outranks

log = structlog.get_logger(__name__)


async def create(
    db: AsyncSession, user_id: uuid.UUID, payload: OrganizationCreate
) -> Organization:
    org = Organization(
        name=payload.name,
        slug=await unique_slug(db, Organization, payload.slug or payload.name),
    )
    db.add(org)
    await db.flush()

    db.add(OrganizationMember(org_id=org.id, user_id=user_id, role=OrgRole.OWNER))
    await db.commit()
    await db.refresh(org)

    log.info("org.created", org_id=str(org.id), user_id=str(user_id))
    return org


async def list_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[Organization]:
    stmt = (
        select(Organization)
        .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(Organization.name)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_detail(
    db: AsyncSession, org_id: uuid.UUID, role: OrgRole
) -> dict:
    """Organization plus the counts the dashboard header displays.

    Both counts are computed as scalar subqueries in a single statement rather
    than as three separate round trips.
    """
    stmt = select(
        Organization,
        select(func.count())
        .select_from(OrganizationMember)
        .where(OrganizationMember.org_id == org_id)
        .scalar_subquery()
        .label("member_count"),
        select(func.count())
        .select_from(Project)
        .where(Project.org_id == org_id)
        .scalar_subquery()
        .label("project_count"),
    ).where(Organization.id == org_id)

    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise NotFoundError("Organization not found")

    org, member_count, project_count = row
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "created_at": org.created_at,
        "my_role": role,
        "member_count": member_count,
        "project_count": project_count,
    }


async def update(
    db: AsyncSession, org_id: uuid.UUID, payload: OrganizationUpdate
) -> Organization:
    org = await db.get(Organization, org_id)
    if org is None:
        raise NotFoundError("Organization not found")

    # `exclude_unset` distinguishes "field omitted" from "field set to null".
    # Without it, a PATCH sending only `name` would blank every other column.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(org, field, value)

    await db.commit()
    await db.refresh(org)
    log.info("org.updated", org_id=str(org_id))
    return org


async def delete_org(db: AsyncSession, org_id: uuid.UUID) -> None:
    """Delete an organization and everything beneath it.

    The cascade chain is enforced by the database, not by this function:
    organization -> projects -> queues -> jobs -> executions/logs. Relying on
    ON DELETE CASCADE means the cleanup is atomic and cannot be left half-done
    by a process that dies midway.
    """
    org = await db.get(Organization, org_id)
    if org is None:
        raise NotFoundError("Organization not found")

    await db.delete(org)
    await db.commit()
    log.warning("org.deleted", org_id=str(org_id))


# =============================================================================
# Members
# =============================================================================


async def list_members(db: AsyncSession, org_id: uuid.UUID) -> list[dict]:
    stmt = (
        select(User.id, User.email, User.full_name, OrganizationMember.role,
               OrganizationMember.created_at)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .where(OrganizationMember.org_id == org_id)
        .order_by(User.full_name)
    )
    return [
        {
            "user_id": r.id,
            "email": r.email,
            "full_name": r.full_name,
            "role": r.role,
            "joined_at": r.created_at,
        }
        for r in (await db.execute(stmt)).all()
    ]


def _assert_may_grant(actor: OrgRole, granted: OrgRole) -> None:
    """Nobody may hand out a role above their own.

    Without this an `admin` can simply set their own row to `owner`, and the
    last-owner guard below does not help: once there are two owners, demoting
    or removing the original is permitted. That is a full takeover of the
    organization by anyone trusted enough to manage members.
    """
    if outranks(granted, actor):
        raise AuthorizationError(
            f"You cannot grant the {granted.value} role; it is above your own "
            f"({actor.value})"
        )


def _assert_may_target(actor: OrgRole, target: OrgRole) -> None:
    """Nobody may change or remove a member who outranks them.

    The mirror of the rule above: blocking upward grants is pointless if an
    admin can instead demote every owner out of the way.
    """
    if outranks(target, actor):
        raise AuthorizationError(
            f"You cannot modify a member with the {target.value} role; it is "
            f"above your own ({actor.value})"
        )


async def add_member(
    db: AsyncSession, org_id: uuid.UUID, payload: MemberAddRequest, actor: OrgRole
) -> dict:
    """Add an existing user to an organization by email."""
    _assert_may_grant(actor, payload.role)

    user = await db.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None:
        raise NotFoundError(
            "No user with that email. They must register before being added."
        )

    if await db.get(OrganizationMember, (org_id, user.id)) is not None:
        raise ConflictError("That user is already a member")

    membership = OrganizationMember(org_id=org_id, user_id=user.id, role=payload.role)
    db.add(membership)
    await db.commit()

    log.info("org.member_added", org_id=str(org_id), user_id=str(user.id))
    return {
        "user_id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": membership.role,
        "joined_at": membership.created_at,
    }


async def _count_owners(db: AsyncSession, org_id: uuid.UUID) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(OrganizationMember)
        .where(
            OrganizationMember.org_id == org_id,
            OrganizationMember.role == OrgRole.OWNER,
        )
    )


async def update_member_role(
    db: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: OrgRole,
    actor: OrgRole,
) -> None:
    membership = await db.get(OrganizationMember, (org_id, user_id))
    if membership is None:
        raise NotFoundError("That user is not a member of this organization")

    _assert_may_grant(actor, role)
    _assert_may_target(actor, membership.role)

    # An organization with no owner is unadministrable: nobody can add members,
    # change roles, or delete it. Block the demotion that would cause it.
    if membership.role == OrgRole.OWNER and role != OrgRole.OWNER:
        if await _count_owners(db, org_id) <= 1:
            raise ValidationError(
                "Cannot demote the last owner. Promote another member first."
            )

    membership.role = role
    await db.commit()
    log.info("org.member_role_changed", org_id=str(org_id), user_id=str(user_id))


async def remove_member(
    db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, actor: OrgRole
) -> None:
    membership = await db.get(OrganizationMember, (org_id, user_id))
    if membership is None:
        raise NotFoundError("That user is not a member of this organization")

    _assert_may_target(actor, membership.role)

    if membership.role == OrgRole.OWNER and await _count_owners(db, org_id) <= 1:
        raise ValidationError(
            "Cannot remove the last owner. Promote another member first."
        )

    await db.execute(
        delete(OrganizationMember).where(
            OrganizationMember.org_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    await db.commit()
    log.info("org.member_removed", org_id=str(org_id), user_id=str(user_id))
