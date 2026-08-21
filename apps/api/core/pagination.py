"""Keyset (cursor) pagination.

OFFSET pagination degrades linearly: `OFFSET 100000 LIMIT 50` makes PostgreSQL
read and discard 100,000 rows before returning anything. On the `jobs` table --
which is expected to hold millions of rows and is exactly what the dashboard's
Job Explorer pages through -- that is the difference between a fast list view
and a timeout.

Keyset pagination instead seeks directly into the index:

    WHERE (created_at, id) < (:cursor_ts, :cursor_id)
    ORDER BY created_at DESC, id DESC
    LIMIT :limit

Cost is constant regardless of how deep the caller has paged. It also cannot
skip or duplicate rows when new jobs are inserted mid-pagination, which OFFSET
does routinely on a live table.

The trade-off, documented in DESIGN-DECISIONS.md: no random access to "page 47"
and no total count. Both are acceptable here -- an operator scrolling a job list
wants "next", not page 47, and an exact count of a table this size is itself an
expensive scan.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

from apps.api.core.config import settings
from apps.api.core.errors import ValidationError

T = TypeVar("T")


class Cursor(BaseModel):
    """Opaque position marker: the sort key of the last row of a page."""

    ts: datetime
    id: uuid.UUID

    def encode(self) -> str:
        """Serialize to a URL-safe token.

        Base64 is used to signal opacity, not for secrecy -- it discourages
        clients from parsing and constructing cursors by hand, which would
        couple them to our sort key and break the moment we change it.
        """
        raw = json.dumps({"ts": self.ts.isoformat(), "id": str(self.id)})
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        try:
            padded = token + "=" * (-len(token) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded).decode())
            return cls(ts=datetime.fromisoformat(data["ts"]), id=uuid.UUID(data["id"]))
        except (
            binascii.Error,
            json.JSONDecodeError,
            KeyError,
            ValueError,
            UnicodeDecodeError,
        ) as exc:
            raise ValidationError(
                "Malformed pagination cursor",
                details={"cursor": token},
            ) from exc


class PageParams(BaseModel):
    """Query parameters controlling a paginated list."""

    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)

    @property
    def decoded_cursor(self) -> Cursor | None:
        return Cursor.decode(self.cursor) if self.cursor else None


def page_params(
    cursor: Annotated[str | None, Query(description="Opaque cursor from a prior page")] = None,
    limit: Annotated[int | None, Query(ge=1, le=200, description="Rows per page")] = None,
) -> PageParams:
    """FastAPI dependency supplying validated pagination parameters."""
    return PageParams(
        cursor=cursor,
        limit=min(limit or settings.default_page_size, settings.max_page_size),
    )


PageQuery = Annotated[PageParams, Query()]


class Page(BaseModel, Generic[T]):
    """Envelope for a page of results.

    `has_more` is derived by fetching limit + 1 rows and discarding the extra --
    which answers "is there a next page?" without a second COUNT query.
    """

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int


def build_page(
    rows: list[Any],
    params: PageParams,
    *,
    ts_attr: str = "created_at",
) -> tuple[list[Any], str | None, bool]:
    """Trim an over-fetched result set and compute the next cursor.

    Callers query `params.limit + 1` rows and pass the result here. Returns the
    rows to actually serve, the cursor for the following page, and whether more
    exist.
    """
    has_more = len(rows) > params.limit
    items = rows[: params.limit]

    next_cursor = None
    if has_more and items:
        last = items[-1]
        ts = getattr(last, ts_attr)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        next_cursor = Cursor(ts=ts, id=last.id).encode()

    return items, next_cursor, has_more
