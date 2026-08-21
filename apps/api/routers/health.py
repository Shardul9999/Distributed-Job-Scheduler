"""Liveness and readiness endpoints.

Two distinct checks, because they answer different questions:

    /health   Is this process running?      -> used by Docker/K8s liveness
    /ready    Can it serve traffic?         -> used by readiness gating

Conflating them causes a classic outage: if the liveness probe also checks the
database, a brief database blip makes the orchestrator kill every healthy API
container, turning a recoverable dependency failure into a total outage.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import text

from apps.api.core.config import settings
from apps.api.core.deps import DbSession

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict:
    """Process is up. Deliberately touches no dependency."""
    return {"status": "ok", "environment": settings.environment}


@router.get("/ready", summary="Readiness probe")
async def ready(db: DbSession) -> dict:
    """Dependencies are reachable. Returns 503 when the database is not."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "database": str(exc)[:200]},
        )
    return {"status": "ready", "database": "connected"}
