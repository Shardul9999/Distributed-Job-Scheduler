"""Response primitives shared across every router."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")

#: Applied to every schema that is constructed from an ORM row.
#: `from_attributes` lets Pydantic read SQLAlchemy model attributes directly,
#: so handlers return ORM objects and FastAPI serializes them through the
#: response_model -- no manual dict-building in any route.
ORM_CONFIG = ConfigDict(from_attributes=True)


class ORMModel(BaseModel):
    model_config = ORM_CONFIG


class PageResponse(BaseModel, Generic[T]):
    """Standard paginated envelope. See core/pagination.py for why keyset."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    request_id: str


class ErrorResponse(BaseModel):
    """Documents the error envelope in the generated OpenAPI schema.

    Declared as a model purely so /docs shows clients the exact failure shape
    rather than leaving them to discover it from a 500.
    """

    error: ErrorDetail


class MessageResponse(BaseModel):
    """Minimal acknowledgement for actions with no resource to return."""

    message: str
