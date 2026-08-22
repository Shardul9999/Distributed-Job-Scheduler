"""Test fixtures: a real PostgreSQL, migrated by the real migrations.

Nothing here is mocked or in-memory, and that is deliberate. The behaviour under
test -- `FOR UPDATE SKIP LOCKED`, partial indexes, advisory locks, `ON CONFLICT`
against a partial unique index -- exists only in PostgreSQL. A test double would
assert that our mock does what we told it to, which is worth nothing. SQLite
would not implement any of it.

The schema is built by running `alembic upgrade head`, not by
`Base.metadata.create_all`. Those two can drift, and if they do it is the
migration that ships. Testing the migration is the point.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.db import (
    Organization,
    Project,
    Queue,
    RetryPolicy,
    User,
    create_engine,
)
from packages.db.enums import RetryStrategy


def _migrate(url: str) -> None:
    """Apply the real migrations to `url`.

    Alembic's env.py builds its own URL from POSTGRES_*, so pointing it
    somewhere new is a matter of setting those -- no second configuration path
    to keep in sync with the first.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    os.environ["POSTGRES_HOST"] = parsed.host or "localhost"
    os.environ["POSTGRES_PORT"] = str(parsed.port or 5432)
    os.environ["POSTGRES_USER"] = parsed.username or "codity"
    os.environ["POSTGRES_PASSWORD"] = parsed.password or ""
    os.environ["POSTGRES_DB"] = parsed.database or "codity"

    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    """A migrated PostgreSQL 16, from whichever source is available.

    `TEST_DATABASE_URL` points the suite at a database that already exists --
    a CI service container, or the compose stack on a developer's machine.
    Without it, Testcontainers starts a throwaway instance. Supporting both is
    what lets the identical suite run inside the API container (where Docker is
    not reachable) and on a laptop (where it is).

    Session-scoped either way: container startup plus migration takes a few
    seconds, and paying that per test produces a suite nobody runs.
    """
    existing = os.getenv("TEST_DATABASE_URL")
    if existing:
        _migrate(existing)
        yield existing
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        url = (
            f"postgresql+asyncpg://{pg.username}:{pg.password}"
            f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
        )
        _migrate(url)
        yield url


@pytest_asyncio.fixture
async def engine(postgres_url: str) -> AsyncGenerator[AsyncEngine, None]:
    eng = create_engine(postgres_url, pool_size=25, max_overflow=25)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        yield session


@pytest_asyncio.fixture
async def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """For tests that need many independent sessions -- i.e. many connections.

    The concurrency tests depend on this. Ten claimers sharing one session
    would serialise on that session's single connection and the test would pass
    without ever exercising SKIP LOCKED.
    """
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def scaffold(db: AsyncSession) -> dict:
    """A fresh user/org/project/policy/queue chain for one test.

    Every fixture uses a unique suffix rather than truncating tables between
    tests: tests then share one migrated database without a teardown step that
    could accidentally run against a real one.
    """
    tag = uuid.uuid4().hex[:8]

    user = User(
        email=f"test-{tag}@example.com",
        password_hash="not-a-real-hash",
        full_name="Test User",
    )
    org = Organization(name=f"Org {tag}", slug=f"org-{tag}")
    db.add_all([user, org])
    await db.flush()

    project = Project(org_id=org.id, name=f"Project {tag}", slug=f"proj-{tag}")
    db.add(project)
    await db.flush()

    policy = RetryPolicy(
        project_id=project.id,
        name=f"policy-{tag}",
        strategy=RetryStrategy.FIXED,
        max_attempts=3,
        base_delay_ms=10,
        max_delay_ms=100,
        jitter=False,
    )
    db.add(policy)
    await db.flush()

    queue = Queue(
        project_id=project.id,
        name=f"queue-{tag}",
        # High enough that the fleet-wide cap never becomes the thing under
        # test. Concurrency-cap behaviour has its own test.
        max_concurrency=100_000,
        visibility_timeout_s=300,
        default_timeout_s=60,
        retry_policy_id=policy.id,
    )
    db.add(queue)
    await db.commit()

    return {
        "user": user,
        "org": org,
        "project": project,
        "policy": policy,
        "queue": queue,
    }
