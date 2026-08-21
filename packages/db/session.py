"""Async engine and session factory, shared by every service.

The API, the workers, the scheduler and the reaper are separate processes but
all speak to the same database through this module. Centralizing engine
construction means connection-pool sizing, statement timeouts and echo settings
are configured once rather than drifting per service.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_database_url(async_driver: bool = True) -> str:
    """Assemble the connection URL from environment variables.

    Alembic needs the same credentials but runs its own (sync-capable) engine,
    hence the `async_driver` switch.
    """
    user = os.getenv("POSTGRES_USER", "codity")
    password = os.getenv("POSTGRES_PASSWORD", "codity_dev_password")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DB", "codity")
    driver = "postgresql+asyncpg" if async_driver else "postgresql"
    return f"{driver}://{user}:{password}@{host}:{port}/{database}"


def create_engine(
    url: str | None = None,
    *,
    pool_size: int = 10,
    max_overflow: int = 5,
    echo: bool = False,
) -> AsyncEngine:
    """Create an async engine with pool settings tuned for this workload.

    `pool_pre_ping` is on because workers are long-lived processes that may sit
    idle between claims; without it, the first query after an idle period can
    fail on a connection the server has already dropped.

    Workers should pass a smaller `pool_size` than the API -- a worker needs
    roughly one connection per concurrent job plus one for heartbeats, whereas
    the API needs one per in-flight request.
    """
    return create_async_engine(
        url or build_database_url(),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=echo,
    )


#: Process-wide engine. Created lazily so that importing this module does not
#: open sockets -- which matters for Alembic and for tests that supply their own.
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine(echo=os.getenv("SQL_ECHO", "").lower() == "true")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            # Attributes stay readable after commit. Without this, returning an
            # ORM object from a FastAPI handler after commit triggers a lazy
            # refresh on a closed session -- the classic MissingGreenlet error.
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. One session per request, always closed."""
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Transactional scope for non-request code (workers, scheduler, scripts).

    Commits on success, rolls back on any exception. The worker's claim path
    deliberately does *not* use this -- it manages its own transaction boundary
    so the claim and the execution-row insert land atomically.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close the pool. Called on service shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
