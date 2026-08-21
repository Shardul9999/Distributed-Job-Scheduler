"""Organization and membership contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from apps.api.schemas.common import ORMModel
from packages.db.enums import OrgRole


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Optional: derived from `name` when omitted. Accepting one lets a caller
    # keep a stable URL slug while renaming the display name.
    slug: str | None = Field(default=None, min_length=1, max_length=100)


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)


class OrganizationResponse(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime


class OrganizationDetailResponse(OrganizationResponse):
    """Adds the caller's own role, so the dashboard can hide actions the user
    cannot perform rather than letting them fail with a 403."""

    my_role: OrgRole
    member_count: int
    project_count: int


class MemberAddRequest(BaseModel):
    email: EmailStr
    role: OrgRole = OrgRole.MEMBER


class MemberUpdateRequest(BaseModel):
    role: OrgRole


class MemberResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: OrgRole
    joined_at: datetime
