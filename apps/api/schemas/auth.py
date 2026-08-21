"""Authentication request/response contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from apps.api.schemas.common import ORMModel


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    # Convenience: registering creates a first organization in the same call,
    # so a new user lands on a usable account instead of an empty shell.
    organization_name: str = Field(min_length=1, max_length=255)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        # Deliberately modest rules. Length dominates entropy, and elaborate
        # composition requirements are well established to push users toward
        # predictable substitutions rather than stronger secrets.
        if v.isdigit() or v.isalpha():
            raise ValueError("Password must contain both letters and numbers")
        return v

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        # Normalized here, at the boundary, so the unique index on users.email
        # is the sole source of truth and cannot be defeated by casing.
        return v.lower().strip()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.lower().strip()


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the access token expires


class UserResponse(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    created_at: datetime


class MembershipSummary(BaseModel):
    org_id: uuid.UUID
    org_name: str
    org_slug: str
    role: str


class MeResponse(BaseModel):
    """`GET /auth/me` -- identity plus the org memberships the UI needs to
    render its navigation, in one round trip instead of two."""

    user: UserResponse
    organizations: list[MembershipSummary]
