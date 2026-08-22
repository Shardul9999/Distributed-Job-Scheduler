"""Server-Sent Events: the dashboard's live feed.

One endpoint, `GET /events`, streaming a periodic whole-system snapshot. SSE
rather than WebSockets because the data flow is strictly server -> client: the
browser never pushes anything back, so a bidirectional socket would buy nothing
and cost a heavier protocol and its own reconnection logic. `EventSource`
reconnects natively.

**Auth over SSE.** `EventSource` cannot set an `Authorization` header, so this
one endpoint accepts the access token as a `token` query parameter. It is
validated exactly as the header form is -- same signature check, same
access-token-type check, same active-user lookup -- so the query placement is a
transport detail, not a weakening. This trade-off is recorded in
DESIGN-DECISIONS.md.

Each tick opens a *short* session rather than holding one for the life of the
stream: a dashboard left open overnight must not pin a pooled connection for
hours.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from apps.api.core.errors import AuthenticationError
from apps.api.core.security import TokenError, decode_token
from apps.api.services import metrics_service, worker_service
from packages.db import User, get_sessionmaker

log = structlog.get_logger(__name__)

router = APIRouter(tags=["live"])


async def _resolve_token_user(token: str) -> User:
    """Validate a query-string access token and return its live, active user.

    Deliberately mirrors `deps.get_current_user`: the only difference is where
    the token comes from.
    """
    try:
        payload = decode_token(token, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, KeyError, ValueError) as exc:
        raise AuthenticationError("Invalid or expired token") from exc

    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("User no longer exists or is disabled")
    return user


async def _snapshot() -> dict:
    """One live payload: fleet health plus recent success/failure rates.

    A short session per call. The overview page merges this with its own polled
    charts, so the snapshot stays small -- the numbers that must feel live, not
    a full metrics dump.
    """
    async with get_sessionmaker()() as session:
        fleet = await worker_service.fleet_stats(session)
        # A five-minute window: "how is it doing right now", not the hour-long
        # trend the charts already show.
        health = await metrics_service.health(session, window_seconds=300)

    return {
        "ts": datetime.now(UTC).isoformat(),
        "fleet": fleet.model_dump(mode="json"),
        "health": health.model_dump(mode="json"),
    }


@router.get(
    "/events",
    summary="Live system snapshot stream (SSE)",
    description=(
        "A `text/event-stream` emitting a whole-system snapshot every "
        "`interval` seconds: fleet size and capacity, in-flight and backlog "
        "depth, scheduler-leader presence, and recent success/failure rates. "
        "Because `EventSource` cannot send headers, the access token is passed "
        "as the `token` query parameter."
    ),
    responses={
        200: {"content": {"text/event-stream": {}}},
        401: {"description": "Missing or invalid token"},
    },
)
async def stream_events(
    request: Request,
    token: Annotated[str, Query(description="Access token (EventSource cannot set headers)")],
    interval: Annotated[float, Query(ge=1, le=30)] = 2.0,
) -> StreamingResponse:
    user = await _resolve_token_user(token)

    async def event_generator():
        log.info("sse.connected", user_id=str(user.id))
        # Tell EventSource how long to wait before reconnecting after a drop.
        yield f"retry: {int(interval * 1000)}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await _snapshot()
                    yield f"event: snapshot\ndata: {json.dumps(payload)}\n\n"
                except Exception:  # noqa: BLE001
                    # One failed tick (a transient DB blip) must not tear down a
                    # long-lived stream; emit a comment and keep going.
                    log.warning("sse.snapshot_failed", exc_info=True)
                    yield ": snapshot unavailable\n\n"
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            # Client went away mid-sleep. Normal; let the stream close.
            raise
        finally:
            log.info("sse.disconnected", user_id=str(user.id))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy buffering (nginx) so events flush immediately rather
            # than being held back until the buffer fills.
            "X-Accel-Buffering": "no",
        },
    )
