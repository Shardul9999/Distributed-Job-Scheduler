"""Complete relational schema for the distributed job scheduler.

Thirteen tables, grouped into four concerns:

    Identity & tenancy   users, organizations, organization_members, projects
    Queue configuration  retry_policies, queues
    Job data             jobs, job_executions, job_logs, scheduled_jobs,
                         dead_letter_queue
    Worker fleet         workers, worker_heartbeats

Cascade policy is deliberate and uniform:

    CASCADE   for ownership edges. Deleting a project genuinely means its
              queues and their jobs should disappear -- they cannot exist
              without it.
    RESTRICT  for referenced configuration. Deleting a retry policy that live
              queues depend on must fail loudly rather than silently strip
              their retry behaviour.
    SET NULL  for observability links. A job outlives the worker that ran it;
              purging old worker rows must not delete job history.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.db.base import Base, TimestampMixin, uuid_pk
from packages.db.enums import (
    ExecutionStatus,
    JobStatus,
    LogLevel,
    OrgRole,
    RetryStrategy,
    WorkerStatus,
)


def pg_enum(python_enum: type, name: str) -> SAEnum:
    """Build a native PostgreSQL ENUM that stores *values*, not member names.

    Without `values_callable`, SQLAlchemy persists `JobStatus.QUEUED` as the
    string "QUEUED". We want the lowercase value "queued" so that raw SQL --
    notably the claim query and the partial index predicates -- reads naturally
    and matches what an operator would type in psql.
    """
    return SAEnum(
        python_enum,
        name=name,
        native_enum=True,
        values_callable=lambda e: [member.value for member in e],
    )


# =============================================================================
# Identity and tenancy
# =============================================================================


class User(Base, TimestampMixin):
    """A person who can authenticate.

    Emails are stored lowercased and uniquely indexed. The alternative -- the
    `citext` extension -- is arguably more correct, but normalizing at the
    service boundary keeps the schema dependency-free and the behaviour
    explicit. Noted in DESIGN-DECISIONS.md.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    memberships: Mapped[list[OrganizationMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Organization(Base, TimestampMixin):
    """Top-level tenant. Owns projects; users join via organization_members."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    members: Mapped[list[OrganizationMember]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    projects: Mapped[list[Project]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class OrganizationMember(Base, TimestampMixin):
    """Junction resolving the many-to-many between users and organizations.

    The composite primary key (org_id, user_id) enforces "one membership per
    user per org" structurally -- no application check needed, and no way to
    create a duplicate even by accident.

    `role` lives here rather than on `users` because authorization is
    per-organization: the same person may own one org and merely view another.
    This single column is what makes the RBAC bonus feature a one-dependency
    change rather than a schema migration.
    """

    __tablename__ = "organization_members"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[OrgRole] = mapped_column(
        pg_enum(OrgRole, "org_role"),
        nullable=False,
        server_default=OrgRole.MEMBER.value,
    )

    organization: Mapped[Organization] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")

    __table_args__ = (
        # Reverse-direction index. The PK already indexes (org_id, user_id),
        # which serves "list members of this org". This one serves the equally
        # common "list orgs this user belongs to", issued on every login.
        Index("idx_org_members_user", "user_id"),
    )


class Project(Base, TimestampMixin):
    """A workload boundary inside an organization. Owns queues."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only the hash is stored -- the plaintext key is shown once at creation and
    # never again, the same policy as the user password column.
    api_key_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    organization: Mapped[Organization] = relationship(back_populates="projects")
    queues: Mapped[list[Queue]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    retry_policies: Mapped[list[RetryPolicy]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Slugs are unique per organization, not globally: two companies may
        # both have a project called "billing".
        UniqueConstraint("org_id", "slug", name="uq_projects_org_slug"),
        Index("idx_projects_org", "org_id"),
    )


# =============================================================================
# Queue configuration
# =============================================================================


class RetryPolicy(Base, TimestampMixin):
    """Reusable backoff configuration.

    Extracted into its own table rather than inlined as columns on `queues`.
    Inlining would be simpler, but it would duplicate the same four values
    across every queue that shares a policy, and changing the org-wide retry
    standard would then mean updating N rows instead of one. This is the
    normalization decision defended in DESIGN-DECISIONS.md.
    """

    __tablename__ = "retry_policies"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy: Mapped[RetryStrategy] = mapped_column(
        pg_enum(RetryStrategy, "retry_strategy"), nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    base_delay_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1000")
    )
    max_delay_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3600000")
    )
    # Full jitter. Without it, N jobs failing against the same downed dependency
    # retry in lockstep and stampede it again the instant it recovers.
    jitter: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    project: Mapped[Project] = relationship(back_populates="retry_policies")
    queues: Mapped[list[Queue]] = relationship(back_populates="retry_policy")

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_retry_policies_project_name"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("base_delay_ms >= 0", name="base_delay_non_negative"),
        CheckConstraint("max_delay_ms >= base_delay_ms", name="max_delay_gte_base"),
    )


class Queue(Base, TimestampMixin):
    """A named work channel with its own priority, concurrency cap and policy."""

    __tablename__ = "queues"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Default priority inherited by jobs that do not specify their own. Higher
    # sorts first in the claim query.
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Fleet-wide cap: the maximum number of jobs from this queue that may be
    # RUNNING across *all* workers simultaneously, not per worker.
    max_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10")
    )
    is_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # How long a claim is honoured before the reaper treats the job as orphaned.
    visibility_timeout_s: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("300")
    )
    default_timeout_s: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("60")
    )
    # Reserved for the rate-limiting bonus; NULL means unlimited.
    rate_limit_per_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # RESTRICT, not CASCADE: deleting a policy that live queues depend on must
    # raise an error rather than quietly leaving those queues without retry
    # behaviour. A loud failure is the correct outcome here.
    retry_policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("retry_policies.id", ondelete="RESTRICT"), nullable=False
    )

    project: Mapped[Project] = relationship(back_populates="queues")
    retry_policy: Mapped[RetryPolicy] = relationship(back_populates="queues")
    jobs: Mapped[list[Job]] = relationship(
        back_populates="queue", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_queues_project_name"),
        CheckConstraint("max_concurrency >= 1", name="max_concurrency_positive"),
        CheckConstraint("visibility_timeout_s > 0", name="visibility_timeout_positive"),
        Index("idx_queues_project", "project_id"),
    )


# =============================================================================
# Jobs
# =============================================================================


class Job(Base, TimestampMixin):
    """The hot table. One row per unit of work.

    Holds *current state only*. Everything historical -- what happened on each
    attempt, which worker ran it, how long it took, what it printed -- lives in
    job_executions and job_logs. That split is the central normalization
    decision of this schema: without it, "retry history" would be an
    ever-growing JSON blob on this row that no query could reach into.
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    queue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queues.id", ondelete="CASCADE"), nullable=False
    )

    job_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"),
        nullable=False,
        server_default=JobStatus.QUEUED.value,
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Snapshotted from the retry policy at enqueue time rather than joined at
    # claim time. A policy edit must not retroactively change the contract of
    # jobs already in flight.
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    timeout_s: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("60")
    )

    # Eligibility time. This single column expresses immediate (now), delayed
    # (now + d) and scheduled (a fixed timestamp) jobs -- no separate columns
    # or job kinds required.
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # SET NULL: a job's history must survive the deletion of the worker row
    # that once ran it.
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )

    # Fencing token, regenerated on every claim. A worker must present the
    # token it was issued in order to write a result. If the reaper has since
    # revived this job and handed it to someone else, the token has changed and
    # the zombie's UPDATE matches zero rows -- so its stale result is discarded
    # instead of overwriting the live attempt. This is what makes recovery from
    # a hung (as opposed to dead) worker actually safe.
    lock_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Client-supplied dedupe key. Uniquely indexed per queue where present, so
    # a retried POST cannot enqueue the same work twice.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    scheduled_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scheduled_jobs.id", ondelete="SET NULL"), nullable=True
    )

    # Reserved for the workflow-dependency bonus: job IDs that must reach
    # COMPLETED before this one becomes claimable. Carried in the schema from
    # day one so that enabling DAGs later is a code change, not a migration on
    # the largest table in the system.
    depends_on: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    queue: Mapped[Queue] = relationship(back_populates="jobs")
    executions: Mapped[list[JobExecution]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    logs: Mapped[list[JobLog]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("timeout_s > 0", name="timeout_positive"),
        # ---------------------------------------------------------------
        # THE claim index. Partial, and that is the whole point.
        #
        # The WHERE clause means only rows a worker could actually claim are
        # present in the index. A jobs table holding ten million COMPLETED
        # rows keeps an index containing just the few thousand pending ones,
        # so claim latency stays flat as history grows instead of degrading
        # with total table size.
        #
        # Column order matches the claim query's ORDER BY exactly
        # (priority DESC, run_at ASC) so PostgreSQL reads the index in order
        # and never sorts.
        #
        # This predicate must stay in sync with CLAIMABLE_STATUSES in
        # enums.py -- if they drift, the claim query silently falls back to a
        # sequential scan.
        # ---------------------------------------------------------------
        Index(
            "idx_jobs_claim",
            "queue_id",
            text("priority DESC"),
            text("run_at ASC"),
            postgresql_where=text("status IN ('queued', 'scheduled')"),
        ),
        # Serves the reaper's scan for stale claims. Also partial: in a healthy
        # system this index is nearly empty, so the sweep is almost free.
        Index(
            "idx_jobs_reaper",
            "claimed_at",
            postgresql_where=text("status IN ('claimed', 'running')"),
        ),
        # Enforces idempotency at the database level. Partial-unique, so the
        # many jobs with no key do not collide with each other on NULL.
        Index(
            "idx_jobs_idempotency",
            "queue_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "idx_jobs_batch",
            "batch_id",
            postgresql_where=text("batch_id IS NOT NULL"),
        ),
        # Serves the dashboard's Job Explorer: filter by queue and status,
        # newest first. Full (not partial) because the explorer's most common
        # use is inspecting completed and dead jobs.
        Index("idx_jobs_explorer", "queue_id", "status", text("created_at DESC")),
        Index("idx_jobs_scheduled_parent", "scheduled_job_id"),
    )


class JobExecution(Base):
    """One row per attempt. Append-only; never updated after completion.

    This is the table that makes "retry history", "execution metrics" and
    "worker assignment" -- three separate assignment requirements -- queryable
    rather than merely displayable. Attempt 1 failing and attempt 2 succeeding
    are two durable rows, not one mutated row.

    No TimestampMixin: an immutable record has no meaningful `updated_at`, and
    `started_at` already carries creation time.
    """

    __tablename__ = "job_executions"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[ExecutionStatus] = mapped_column(
        pg_enum(ExecutionStatus, "execution_status"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Denormalized on purpose. It is derivable from the two timestamps above,
    # but the throughput and latency charts aggregate over it on every refresh,
    # and computing an interval per row at query time is measurably slower than
    # summing an integer column.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    job: Mapped[Job] = relationship(back_populates="executions")

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_executions_job_attempt"),
        Index("idx_exec_job", "job_id", text("attempt_number DESC")),
        Index("idx_exec_worker", "worker_id"),
        # Drives the throughput chart: "executions per interval, by outcome".
        Index("idx_exec_metrics", text("started_at DESC"), "status"),
    )


class JobLog(Base):
    """Log lines emitted by a job during execution.

    `bigserial` rather than uuid: this is the highest-volume table in the
    system and a monotonically increasing key keeps inserts appending to the
    rightmost index page instead of scattering random writes across the B-tree.
    """

    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_executions.id", ondelete="CASCADE"), nullable=True
    )
    level: Mapped[LogLevel] = mapped_column(
        pg_enum(LogLevel, "log_level"),
        nullable=False,
        server_default=LogLevel.INFO.value,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Mapped to the column name "metadata"; the Python attribute is renamed
    # because `metadata` is reserved on SQLAlchemy's declarative base.
    log_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    logged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="logs")

    __table_args__ = (
        Index("idx_logs_job", "job_id", text("logged_at DESC")),
        Index("idx_logs_execution", "execution_id"),
    )


class ScheduledJob(Base, TimestampMixin):
    """A recurring job *template*, not a job.

    The scheduler reads rows whose `next_run_at` has passed, inserts a concrete
    `jobs` row for each, and advances `next_run_at` via croniter. Keeping the
    template distinct from its instances is what allows the dashboard to show
    "this cron has fired 400 times, 3 of them failed" -- the 400 job rows all
    carry `scheduled_job_id` pointing back here.
    """

    __tablename__ = "scheduled_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    queue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queues.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    # IANA zone name. Stored per schedule because "every day at 09:00" means
    # different UTC instants in different offices, and must survive DST.
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'UTC'")
    )

    job_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("queue_id", "name", name="uq_scheduled_jobs_queue_name"),
        # The scheduler's only hot query: "which templates are due?". Partial on
        # is_active so paused schedules cost nothing to skip.
        Index(
            "idx_sched_due",
            "next_run_at",
            postgresql_where=text("is_active"),
        ),
    )


class DeadLetterEntry(Base):
    """A job that exhausted its retries. Terminal, and human-inspectable.

    Replay inserts a *new* job and records its id in `replayed_job_id` rather
    than resurrecting the original. The forensic record stays intact -- you can
    always see what failed, how many times, and what was done about it.
    """

    __tablename__ = "dead_letter_queue"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    queue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queues.id", ondelete="CASCADE"), nullable=False
    )

    job_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # Copied, not joined. The DLQ must remain readable as a standalone record
    # even if the originating job row is later purged by a retention policy.
    original_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False)
    error_stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_attempts: Mapped[int] = mapped_column(Integer, nullable=False)

    died_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    replayed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replayed_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    # Populated by the AI failure-summary bonus feature.
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_dlq_queue", "queue_id", text("died_at DESC")),
        Index("idx_dlq_job", "job_id"),
        # Serves the dashboard's default DLQ view: unresolved entries only.
        Index(
            "idx_dlq_unreplayed",
            text("died_at DESC"),
            postgresql_where=text("replayed_at IS NULL"),
        ),
    )


# =============================================================================
# Worker fleet
# =============================================================================


class Worker(Base):
    """A live worker process. One row per running process, self-registered.

    `last_heartbeat_at` is the liveness signal the reaper reads. Keeping it on
    this small current-state table -- rather than deriving it from the
    heartbeat time series -- means the liveness check is an indexed scan of a
    handful of rows rather than an aggregate over history.
    """

    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )

    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    # Which queues this worker polls. An array rather than a junction table:
    # it is read on every claim, never queried *by*, and rewritten only on
    # worker restart -- a join here would buy normalization we never use.
    queue_names: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)

    status: Mapped[WorkerStatus] = mapped_column(
        pg_enum(WorkerStatus, "worker_status"),
        nullable=False,
        server_default=WorkerStatus.STARTING.value,
    )
    jobs_processed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    heartbeats: Mapped[list[WorkerHeartbeat]] = relationship(
        back_populates="worker", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("concurrency >= 1", name="concurrency_positive"),
        # The reaper's liveness sweep. Partial on active workers only -- dead
        # and stopped rows are history and never need re-checking.
        Index(
            "idx_workers_alive",
            "last_heartbeat_at",
            postgresql_where=text("status IN ('active', 'draining')"),
        ),
        Index("idx_workers_project", "project_id"),
    )


class WorkerHeartbeat(Base):
    """Time-series liveness and utilisation samples.

    Split from `workers` so the current-state row stays small and hot while
    history grows unbounded. `bigserial` for the same append-locality reason as
    job_logs.

    A maintenance task trims this to 24 hours. At real scale the answer is
    declarative partitioning by day (pg_partman), which turns retention into a
    DROP PARTITION instead of a bulk DELETE -- noted as the scale-out path in
    DESIGN-DECISIONS.md.
    """

    __tablename__ = "worker_heartbeats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    worker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), nullable=False
    )
    beat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    active_jobs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    jobs_processed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    cpu_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)

    worker: Mapped[Worker] = relationship(back_populates="heartbeats")

    __table_args__ = (Index("idx_hb_worker", "worker_id", text("beat_at DESC")),)
