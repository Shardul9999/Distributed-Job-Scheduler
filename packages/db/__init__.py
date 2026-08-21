"""Shared database package: schema, enums, and session management."""

from packages.db.base import Base
from packages.db.enums import (
    CLAIMABLE_STATUSES,
    IN_FLIGHT_STATUSES,
    TERMINAL_STATUSES,
    ExecutionStatus,
    JobStatus,
    LogLevel,
    OrgRole,
    RetryStrategy,
    WorkerStatus,
)
from packages.db.models import (
    DeadLetterEntry,
    Job,
    JobExecution,
    JobLog,
    Organization,
    OrganizationMember,
    Project,
    Queue,
    RetryPolicy,
    ScheduledJob,
    User,
    Worker,
    WorkerHeartbeat,
)
from packages.db.session import (
    build_database_url,
    create_engine,
    dispose_engine,
    get_engine,
    get_session,
    get_sessionmaker,
    session_scope,
)

__all__ = [
    "Base",
    # enums
    "CLAIMABLE_STATUSES",
    "IN_FLIGHT_STATUSES",
    "TERMINAL_STATUSES",
    "ExecutionStatus",
    "JobStatus",
    "LogLevel",
    "OrgRole",
    "RetryStrategy",
    "WorkerStatus",
    # models
    "DeadLetterEntry",
    "Job",
    "JobExecution",
    "JobLog",
    "Organization",
    "OrganizationMember",
    "Project",
    "Queue",
    "RetryPolicy",
    "ScheduledJob",
    "User",
    "Worker",
    "WorkerHeartbeat",
    # session
    "build_database_url",
    "create_engine",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "session_scope",
]
