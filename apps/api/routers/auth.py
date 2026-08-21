"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from apps.api.core.deps import CurrentUser, DbSession
from apps.api.schemas.auth import (
    LoginRequest,
    MeResponse,
    MembershipSummary,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserResponse,
)
from apps.api.schemas.common import ErrorResponse
from apps.api.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


class _RegisterResponse(TokenPair):
    user: UserResponse


@router.post(
    "/register",
    response_model=_RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse, "description": "Email already registered"}},
    summary="Create an account and its first organization",
)
async def register(payload: RegisterRequest, db: DbSession) -> _RegisterResponse:
    user, tokens = await auth_service.register(db, payload)
    return _RegisterResponse(
        **tokens.model_dump(), user=UserResponse.model_validate(user)
    )


class _LoginResponse(TokenPair):
    user: UserResponse


@router.post(
    "/login",
    response_model=_LoginResponse,
    responses={401: {"model": ErrorResponse, "description": "Invalid credentials"}},
    summary="Exchange credentials for a token pair",
)
async def login(payload: LoginRequest, db: DbSession) -> _LoginResponse:
    user, tokens = await auth_service.login(db, payload)
    return _LoginResponse(**tokens.model_dump(), user=UserResponse.model_validate(user))


@router.post(
    "/refresh",
    response_model=TokenPair,
    responses={401: {"model": ErrorResponse, "description": "Invalid refresh token"}},
    summary="Rotate an expiring token pair",
)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    return await auth_service.refresh(db, payload.refresh_token)


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Current user and their organization memberships",
)
async def me(user: CurrentUser, db: DbSession) -> MeResponse:
    rows = await auth_service.list_memberships(db, user.id)
    return MeResponse(
        user=UserResponse.model_validate(user),
        organizations=[
            MembershipSummary(
                org_id=r.id, org_name=r.name, org_slug=r.slug, role=r.role.value
            )
            for r in rows
        ],
    )
