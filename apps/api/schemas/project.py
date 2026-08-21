"""Project contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from apps.api.schemas.common import ORMModel


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class ProjectResponse(ORMModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ProjectDetailResponse(ProjectResponse):
    """Queue counts included so the project list can show workload at a glance
    without the dashboard issuing one request per project."""

    queue_count: int


class ApiKeyResponse(BaseModel):
    """Returned exactly once, at rotation.

    Only the hash is persisted, so this plaintext value cannot be recovered
    later -- the same policy the schema applies to user passwords.
    """

    api_key: str
    message: str = "Store this key now. It will not be shown again."
