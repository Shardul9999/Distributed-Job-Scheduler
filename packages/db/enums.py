"""Domain enumerations.

These are emitted as native PostgreSQL ENUM types rather than as CHECK
constraints or plain text columns. Native enums give us database-level
validation (an invalid status literally cannot be written, even by a stray
psql session), a compact on-disk representation, and a self-documenting schema
that tools like `\\dT+` and ER diagram generators can introspect.

The trade-off, noted in DESIGN-DECISIONS.md: adding a value to a native enum
requires an `ALTER TYPE ... ADD VALUE` migration rather than a plain code
change. That cost is acceptable here because these state machines are
deliberately closed sets -- an unplanned job status is a bug, not a feature.
"""

from __future__ import annotations

import enum


class OrgRole(str, enum.Enum):
    """Role a user holds *within a specific organization*.

    Held on the membership junction rather than on `users` so that one person
    can be an owner of one organization and a viewer of another. This is what
    makes the RBAC bonus a single dependency check.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


#: Privilege ordering. A check for MEMBER is satisfied by ADMIN and OWNER.
#:
#: Lives beside the enum rather than inside the API's dependency module because
#: two separate rules need it: "may you perform this action?" (authorization)
#: and "may you grant or revoke this role?" (administration). Keeping one
#: ordering means those two can never disagree about which role outranks which.
ROLE_RANK: dict[OrgRole, int] = {
    OrgRole.VIEWER: 0,
    OrgRole.MEMBER: 1,
    OrgRole.ADMIN: 2,
    OrgRole.OWNER: 3,
}


def outranks(a: OrgRole, b: OrgRole) -> bool:
    """True when `a` sits strictly above `b`."""
    return ROLE_RANK[a] > ROLE_RANK[b]


class RetryStrategy(str, enum.Enum):
    """Backoff curve applied between attempts.

    Required by the assignment: "configurable retry strategies such as fixed
    delay, linear backoff, and exponential backoff".
    """

    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


class JobStatus(str, enum.Enum):
    """Job lifecycle states.

    The assignment specifies: Queued -> Scheduled -> Claimed -> Running ->
    Completed, with retries and DLQ. We model both QUEUED and SCHEDULED as
    distinct states -- SCHEDULED means "exists but not yet eligible", QUEUED
    means "eligible now" -- and the claim predicate accepts either so long as
    `run_at <= now()`. See DESIGN-DECISIONS.md for why this reading was chosen.

    FAILED is a transient state between attempts; DEAD is terminal and always
    accompanied by a dead_letter_queue row.
    """

    QUEUED = "queued"
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


#: States from which a worker may claim a job. Mirrors the partial-index
#: predicate on `idx_jobs_claim` -- if you change one, change both, or the
#: claim query silently stops using the index.
CLAIMABLE_STATUSES = (JobStatus.QUEUED, JobStatus.SCHEDULED)

#: States that mean "a worker currently believes it owns this job". The reaper
#: scans exactly these looking for stale `claimed_at` timestamps.
IN_FLIGHT_STATUSES = (JobStatus.CLAIMED, JobStatus.RUNNING)

#: Terminal states. A job here will never be claimed again without explicit
#: operator action (retry or DLQ replay).
TERMINAL_STATUSES = (JobStatus.COMPLETED, JobStatus.DEAD, JobStatus.CANCELLED)


class ExecutionStatus(str, enum.Enum):
    """Outcome of a single attempt.

    LOST is written by the reaper, not by a worker: it means the worker holding
    this job stopped heartbeating and we recovered the job on its behalf. Giving
    it a distinct value keeps crash-recovery visible in the execution history
    instead of disguising infrastructure failure as application failure.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    LOST = "lost"


class WorkerStatus(str, enum.Enum):
    """Worker fleet states.

    DRAINING is the graceful-shutdown state: the worker has received SIGTERM,
    has stopped claiming new work, and is finishing what it already holds.
    DEAD is assigned by the reaper (missed heartbeats); STOPPED is written by
    the worker itself on a clean exit. The distinction tells an operator whether
    a worker left politely or was killed.
    """

    STARTING = "starting"
    ACTIVE = "active"
    DRAINING = "draining"
    DEAD = "dead"
    STOPPED = "stopped"


class LogLevel(str, enum.Enum):
    """Severity for per-job log lines surfaced in the dashboard."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
