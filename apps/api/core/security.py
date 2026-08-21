"""Password hashing and JWT issuance/verification.

Argon2id is used rather than bcrypt. It won the Password Hashing Competition,
it is memory-hard (which raises the cost of GPU cracking far more than bcrypt's
CPU-bound work factor does), and unlike bcrypt it has no 72-byte input truncation
surprise. `argon2-cffi` is used directly rather than through passlib, which adds
a dependency layer and has a history of bcrypt backend incompatibilities.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from apps.api.core.config import settings

_hasher = PasswordHasher()

TokenType = Literal["access", "refresh"]


# =============================================================================
# Passwords
# =============================================================================


def hash_password(plain: str) -> str:
    """Hash a password for storage. Salt is generated and embedded by argon2."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Check a password against its stored hash.

    Returns False rather than raising on a malformed hash, so that a corrupted
    row cannot turn a failed login into a 500.
    """
    try:
        _hasher.verify(hashed, plain)
        return True
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True if the stored hash used weaker parameters than we now require.

    Called on successful login so that tightening the argon2 cost parameters
    later transparently upgrades users as they sign in.
    """
    try:
        return _hasher.check_needs_rehash(hashed)
    except (InvalidHashError, ValueError):
        return True


# =============================================================================
# JSON Web Tokens
# =============================================================================


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        # Unique token id. Gives us the hook for a revocation list later
        # without reissuing every outstanding token.
        "jti": str(uuid.uuid4()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID | str, **extra: Any) -> str:
    """Short-lived credential presented on every request."""
    return _create_token(
        str(user_id),
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
        extra,
    )


def create_refresh_token(user_id: uuid.UUID | str) -> str:
    """Long-lived credential used only against /auth/refresh.

    Access and refresh tokens are distinguished by a `type` claim that is
    checked on decode. Without that check, a stolen refresh token would be
    usable as a 7-day access token -- defeating the point of short access
    expiry entirely.
    """
    return _create_token(
        str(user_id),
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
    )


class TokenError(Exception):
    """Raised when a token is absent, malformed, expired, or the wrong type."""


def decode_token(token: str, expected_type: TokenType = "access") -> dict[str, Any]:
    """Verify signature and expiry, and enforce the token type.

    Raises TokenError with a caller-safe message. The underlying PyJWT
    exception is deliberately not surfaced to the client.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid") from exc

    if payload.get("type") != expected_type:
        raise TokenError(f"Expected a {expected_type} token")

    if not payload.get("sub"):
        raise TokenError("Token is missing a subject")

    return payload
