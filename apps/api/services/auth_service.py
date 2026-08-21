"""Registration, login, and token refresh.

Business logic lives here rather than in the router. Routers stay responsible
for HTTP concerns only -- status codes, response models, dependency wiring --
which keeps them thin and makes this logic testable without spinning up an app.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.core.config import settings
from apps.api.core.errors import AuthenticationError, ConflictError
from apps.api.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from apps.api.schemas.auth import LoginRequest, RegisterRequest, TokenPair
from apps.api.services.slugs import unique_slug
from packages.db import Organization, OrganizationMember, OrgRole, User

log = structlog.get_logger(__name__)


def _issue_tokens(user_id: uuid.UUID) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
        expires_in=settings.access_token_expire_minutes * 60,
    )


async def register(db: AsyncSession, payload: RegisterRequest) -> tuple[User, TokenPair]:
    """Create a user, their first organization, and an owner membership.

    All three inserts share one transaction. A partial failure that left a user
    with no organization would produce an account that cannot create anything
    and cannot be repaired through the API.
    """
    existing = await db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise ConflictError("An account with that email already exists")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(user)
    # Flush, not commit: assigns the server-generated UUID so the membership
    # below can reference it, while keeping everything in one transaction.
    await db.flush()

    org = Organization(
        name=payload.organization_name,
        slug=await unique_slug(db, Organization, payload.organization_name),
    )
    db.add(org)
    await db.flush()

    # The creator owns the organization they just created.
    db.add(OrganizationMember(org_id=org.id, user_id=user.id, role=OrgRole.OWNER))

    await db.commit()
    await db.refresh(user)

    log.info("auth.registered", user_id=str(user.id), org_id=str(org.id))
    return user, _issue_tokens(user.id)


async def login(db: AsyncSession, payload: LoginRequest) -> tuple[User, TokenPair]:
    """Verify credentials and issue a token pair."""
    user = await db.scalar(select(User).where(User.email == payload.email))

    # Identical error for "no such user" and "wrong password". Distinguishing
    # them turns the login endpoint into an account-enumeration oracle.
    if user is None or not verify_password(payload.password, user.password_hash):
        log.warning("auth.login_failed", email=payload.email)
        raise AuthenticationError("Incorrect email or password")

    if not user.is_active:
        raise AuthenticationError("User account is disabled")

    # Transparently upgrade hashes written under weaker argon2 parameters.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
        await db.commit()
        log.info("auth.password_rehashed", user_id=str(user.id))

    log.info("auth.login", user_id=str(user.id))
    return user, _issue_tokens(user.id)


async def refresh(db: AsyncSession, refresh_token: str) -> TokenPair:
    """Exchange a valid refresh token for a new pair.

    `decode_token` enforces `type == "refresh"`, so an access token cannot be
    replayed here to extend a session indefinitely. Both tokens are reissued
    (rotation), which bounds the useful lifetime of a leaked refresh token.
    """
    try:
        payload = decode_token(refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise AuthenticationError("User no longer exists or is disabled")

    return _issue_tokens(user.id)


async def list_memberships(db: AsyncSession, user_id: uuid.UUID) -> list[tuple]:
    """Organizations the user belongs to, with their role in each.

    Uses the `idx_org_members_user` index added in models.py specifically for
    this query -- it runs on every page load of the dashboard.
    """
    stmt = (
        select(
            Organization.id,
            Organization.name,
            Organization.slug,
            OrganizationMember.role,
        )
        .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(Organization.name)
    )
    return list((await db.execute(stmt)).all())
