"""Project operations."""

from __future__ import annotations

import secrets
import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.errors import NotFoundError
from apps.api.core.security import hash_password
from apps.api.schemas.project import ProjectCreate, ProjectUpdate
from apps.api.services.slugs import unique_slug
from packages.db import Project, Queue

log = structlog.get_logger(__name__)

#: Prefix makes keys identifiable in logs and grep-able in leaked-secret scans.
API_KEY_PREFIX = "cdty_"


def generate_api_key() -> str:
    """Create a project API key.

    32 bytes of `secrets` entropy. This will be the credential a worker or a
    producer service uses instead of a user JWT, so it is generated from a
    CSPRNG and stored only as a hash.
    """
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


async def create(
    db: AsyncSession, org_id: uuid.UUID, payload: ProjectCreate
) -> Project:
    project = Project(
        org_id=org_id,
        name=payload.name,
        # Scoped uniqueness: two organizations may each have a "billing"
        # project, matching the uq_projects_org_slug constraint.
        slug=await unique_slug(
            db,
            Project,
            payload.slug or payload.name,
            scope_field="org_id",
            scope_value=org_id,
        ),
        description=payload.description,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    log.info("project.created", project_id=str(project.id), org_id=str(org_id))
    return project


async def list_for_org(db: AsyncSession, org_id: uuid.UUID) -> list[dict]:
    """Projects with their queue counts, in one query.

    The count is a correlated scalar subquery rather than a GROUP BY join, so
    projects with zero queues still appear (a plain inner join would drop them).
    """
    queue_count = (
        select(func.count())
        .select_from(Queue)
        .where(Queue.project_id == Project.id)
        .correlate(Project)
        .scalar_subquery()
    )

    stmt = (
        select(Project, queue_count.label("queue_count"))
        .where(Project.org_id == org_id)
        .order_by(Project.name)
    )

    return [
        {
            "id": p.id,
            "org_id": p.org_id,
            "name": p.name,
            "slug": p.slug,
            "description": p.description,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            "queue_count": qc,
        }
        for p, qc in (await db.execute(stmt)).all()
    ]


async def update(
    db: AsyncSession, project_id: uuid.UUID, payload: ProjectUpdate
) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)

    await db.commit()
    await db.refresh(project)
    log.info("project.updated", project_id=str(project_id))
    return project


async def delete_project(db: AsyncSession, project_id: uuid.UUID) -> None:
    """Delete a project and, by cascade, its queues, jobs and history."""
    project = await db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project not found")

    await db.delete(project)
    await db.commit()
    log.warning("project.deleted", project_id=str(project_id))


async def rotate_api_key(db: AsyncSession, project_id: uuid.UUID) -> str:
    """Issue a new API key, returning the plaintext exactly once.

    Argon2 is reused for the key hash. A random 32-byte token does not need a
    slow hash the way a human password does, but reusing one verified primitive
    is worth more here than the microseconds a fast hash would save.
    """
    project = await db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project not found")

    key = generate_api_key()
    project.api_key_hash = hash_password(key)
    await db.commit()

    log.info("project.api_key_rotated", project_id=str(project_id))
    return key
