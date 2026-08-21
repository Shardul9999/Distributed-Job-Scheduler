"""Slug generation with collision handling."""

from __future__ import annotations

import re
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(value: str, max_length: int = 100) -> str:
    """Lowercase, hyphenate, strip anything else."""
    slug = _NON_SLUG.sub("-", value.lower()).strip("-")
    slug = slug[:max_length].rstrip("-")
    # A name of only punctuation ("!!!") would otherwise yield an empty slug
    # and violate the NOT NULL constraint.
    return slug or f"item-{secrets.token_hex(4)}"


async def unique_slug(
    db: AsyncSession,
    model: type,
    base: str,
    *,
    scope_field: str | None = None,
    scope_value=None,
    max_length: int = 100,
) -> str:
    """Return `base`, or `base-2`, `base-3`, ... until unused.

    This is a best-effort convenience, not the uniqueness guarantee -- two
    concurrent requests can both read "free" and then both insert. The real
    guarantee is the unique constraint in the database, whose IntegrityError is
    translated to a 409 by the handler in core/errors.py. Checking here simply
    means the common case gets a friendly auto-suffixed slug instead of an
    error.
    """
    slug = slugify(base, max_length)
    candidate = slug
    suffix = 2

    while True:
        stmt = select(func.count()).select_from(model).where(model.slug == candidate)
        if scope_field is not None:
            stmt = stmt.where(getattr(model, scope_field) == scope_value)

        if (await db.execute(stmt)).scalar_one() == 0:
            return candidate

        tail = f"-{suffix}"
        candidate = f"{slug[: max_length - len(tail)]}{tail}"
        suffix += 1

        # Pathological contention: stop guessing and use a random suffix.
        if suffix > 100:
            return f"{slug[: max_length - 9]}-{secrets.token_hex(4)}"
