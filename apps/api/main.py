"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.core.config import settings
from apps.api.core.errors import register_exception_handlers
from apps.api.core.logging import RequestContextMiddleware, configure_logging
from apps.api.routers import auth, health, organizations, projects
from packages.db import dispose_engine

configure_logging(level=settings.log_level, json_output=settings.is_production)
log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup and shutdown.

    Disposing the connection pool on shutdown matters more than it looks: with
    three worker containers, a scheduler and N API replicas all holding pools
    against one PostgreSQL instance, leaked connections from restarted
    containers would accumulate toward max_connections.
    """
    log.info(
        "api.starting",
        environment=settings.environment,
        database=f"{settings.postgres_host}:{settings.postgres_port}",
    )
    yield
    log.info("api.stopping")
    await dispose_engine()


app = FastAPI(
    title="Codity Distributed Job Scheduler",
    description=(
        "A distributed job scheduling platform. Jobs are queued in PostgreSQL "
        "and claimed atomically by a fleet of workers using `FOR UPDATE SKIP "
        "LOCKED`, with configurable retry policies, cron scheduling, dead-letter "
        "handling and live fleet observability."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# Order matters. Middleware is applied bottom-up, so RequestContextMiddleware is
# registered last in order to run first -- every log line emitted downstream,
# including CORS rejections, then carries a request id.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Exposed so the dashboard can read the id off a response and show it in an
    # error toast for support.
    expose_headers=["X-Request-ID"],
)
app.add_middleware(RequestContextMiddleware)

register_exception_handlers(app)

# Health checks sit at the root, outside the versioned prefix: an orchestrator
# probing liveness should not have to track the API version.
app.include_router(health.router)

app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(organizations.router, prefix=settings.api_prefix)
app.include_router(projects.router, prefix=settings.api_prefix)


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "codity-job-scheduler",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
    }
