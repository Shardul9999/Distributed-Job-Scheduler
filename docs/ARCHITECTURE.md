# Architecture

A distributed job scheduler built on one principle: **PostgreSQL is the queue,
not a database sitting next to one.** Every hard guarantee the system makes —
a job runs exactly once, a dead worker's jobs come back, a cron fires once per
occurrence, a leader is elected without split-brain — is a property of a single
SQL statement run against Postgres, not of application code coordinating across
services. That decision is defended in `DESIGN-DECISIONS.md §1`; this document
describes the shape that follows from it.

---

## 1. Four process types, one database

```
                          ┌──────────────────────────────┐
                          │          PostgreSQL 16         │
                          │  jobs · executions · queues ·  │
                          │  schedules · DLQ · workers ·   │
                          │  advisory locks · SKIP LOCKED  │
                          └───▲────────▲────────▲──────▲───┘
                              │        │        │      │
        claim / result        │        │        │      │  snapshots
        (SKIP LOCKED)         │        │        │      │  (read-only)
              ┌───────────────┘        │        │      └───────────────┐
              │                        │        │                      │
      ┌───────┴────────┐      ┌────────┴───┐ ┌──┴─────────────┐  ┌─────┴──────┐
      │   worker × N   │      │ scheduler  │ │      api       │  │    web     │
      │  claim loop +  │      │  × 2       │ │   FastAPI      │  │  Next.js   │
      │  executor +    │      │ (1 leader) │ │  58 endpoints  │  │  dashboard │
      │  heartbeat     │      │ cron+reaper│ │  + SSE feed    │  │  (browser) │
      └────────────────┘      └────────────┘ └───────▲────────┘  └─────┬──────┘
                                                      │   REST + SSE    │
                                                      └─────────────────┘
```

The four process types are **independently scalable and share nothing but the
database**:

| Process | Role | Scaling | Coordination |
|---|---|---|---|
| **api** | REST surface, auth, validation, SSE feed, read aggregates | stateless, scale horizontally behind a load balancer | none needed |
| **worker** | claims jobs, executes handlers, writes results/executions/logs | `docker compose up --scale worker=N` | `SKIP LOCKED` — the database hands each claimer a disjoint set |
| **scheduler** | cron materialisation + crash recovery (reaper) | run ≥2 replicas; exactly one acts | `pg_try_advisory_lock` leader election |
| **web** | operator dashboard, six pages, live via SSE | static/SSR, any number | talks to `api` only |

One Docker image is built for all Python process types (`target: dev`); the
`command:` selects which one boots. `web` is the only separate image.

### Why these four and not one

The three things that must happen *fleet-wide and exactly once* — recovering a
crashed worker's jobs, and firing a cron occurrence — cannot live in the worker,
because there are N workers and the action must happen once. They cannot live in
the API, because the API is request-driven and may have zero or fifty instances.
So they get a **fourth process type that is a leader-elected singleton**. Two
scheduler replicas run for availability; the advisory lock guarantees only one is
ever the leader, with no lease, no TTL, and no split-brain window (§4).

---

## 2. Module layering

The codebase is a monorepo with a shared `packages/` layer that the process
types import, so the API, the worker, and the tests all execute *the same* SQL
and *the same* retry maths — never a re-implementation that could drift.

```
packages/                     shared, imported by every process
├── db/
│   ├── models.py             13 SQLAlchemy 2.0 models
│   ├── claim.py              CLAIM_SQL, complete/retry/EXHAUST — the hot path
│   ├── enums.py              native enums + CLAIMABLE_STATUSES (index-synced)
│   ├── session.py            async engine/session factory (pool_pre_ping)
│   └── migrations/           Alembic — the schema that actually ships
├── retry.py                  fixed/linear/exponential + full jitter
└── locks.py                  advisory-lock keys (shared by scheduler + api)

apps/
├── api/                      FastAPI
│   ├── routers/              thin HTTP layer, one file per resource
│   ├── services/             all business logic; routers call into here
│   ├── schemas/              Pydantic request/response models
│   └── core/                 config, deps (auth+RBAC), errors, pagination, logging
├── worker/                   claim loop, executor (semaphore+ProcessPool), handlers
└── scheduler/                leader election, cron.py, reaper.py
```

The **router → service → model** split is strict: routers do HTTP and nothing
else; services own the transactions and the invariants; models are the schema.
Authorization is a FastAPI dependency (`require_role`, `get_accessible_project`)
that runs *before* the handler body, so a route cannot forget its own check.

---

## 3. The job lifecycle

A job is a row whose `status` walks a state machine. Current state lives in
`jobs`; every *attempt* is an immutable row in `job_executions`. The two-table
split is deliberate (`DESIGN-DECISIONS.md §4`): the job answers "what now?", the
executions answer "what happened, every time".

```
                 enqueue
    (immediate) ────────► queued ──────────┐
                                           │ claim (SKIP LOCKED,
    (delayed/cron) ─────► scheduled ───────┤  stamps lock_token)
                                           ▼
                                        claimed ──► running
                                                       │
                    ┌──────────────────────────────────┼─────────────────────┐
                    │ handler ok         handler raises │      worker dies /   │
                    ▼                    ▼               │      visibility exp. │
                completed              (attempts left?)  │            ▼         │
                                        │        │       │      reaper: LOST    │
                                    yes │        │ no    │      execution row,  │
                                        ▼        ▼       │      lock_token=NULL │
                                     queued     dead ◄───┘            │         │
                                   (backoff)  + DLQ row     (attempt consumed)  │
                                        ▲                              │        │
                                        └──────────────────────────────┘        │
                    cancel (pre-terminal) ─► cancelled                          │
```

Two failure paths, deliberately asymmetric:

- **Handler raised** → the code failed. Record a `failed` execution, apply the
  retry policy's backoff, requeue — or, if attempts are exhausted, flip to
  `dead` and write the DLQ row *in the same statement* (`EXHAUST_SQL`).
- **Worker vanished** → infrastructure failed. The reaper records a `lost`
  execution and requeues, but **consumes the attempt** — a lost job may have had
  side effects, so it must not retry forever. `lost` vs `failed` is why the
  distinction exists.

Both terminal-failure writers (worker and reaper) funnel into the same DLQ, and
both are fenced by `lock_token` so a revived zombie cannot dead-letter a job that
has already been handed to someone else.

---

## 4. The four reliability guarantees, and where each lives

| Guarantee | Mechanism | Where |
|---|---|---|
| **No job runs twice** | `SELECT … FOR UPDATE SKIP LOCKED` inside the claim CTE; every result write fenced by `lock_token` | `packages/db/claim.py` |
| **A dead worker's jobs recover** | heartbeats + reaper: workers silent past the timeout → `dead`; their in-flight jobs → `lost` + requeue/DLQ | `apps/scheduler/reaper.py` |
| **A cron fires once per occurrence** | croniter in the schedule's IANA tz; each occurrence keyed `cron:<id>:<fire>` into the partial-unique idempotency index | `apps/scheduler/cron.py` |
| **Exactly one scheduler acts** | `pg_try_advisory_lock` on a dedicated, session-scoped connection — dies with the connection, so no lease to expire | `apps/scheduler/main.py`, `packages/locks.py` |

**Claiming** is per-queue and looped in the worker. A single cross-queue claim
would need a window function to apply each queue's concurrency cap, and Postgres
forbids `FOR UPDATE` alongside window functions — documented as a trade-off.

**Leader election** holds the advisory lock for the process lifetime on its own
connection. A loser logs `scheduler.standby` and retries every
`SCHEDULER_LOCK_RETRY_S`. Because the lock is *session-scoped*, a `kill -9` on the
leader drops its connection and releases the lock automatically — a standby
acquires it in ~5 s, unattended, with no fencing token dance. The API reads
`pg_locks` directly to report `scheduler_leader_present`, so a crashed scheduler
cannot self-report as healthy.

---

## 5. Worker internals — where "concurrency" is demonstrated

A single worker process runs three concurrent loops on one asyncio event loop:

1. **Claim loop** — polls each queue with idle backoff (100 ms → 2 s), claiming
   up to the fleet-wide headroom. Backoff keeps an idle fleet from hammering the
   DB; the ceiling keeps latency bounded when work arrives.
2. **Executor** — an `asyncio.Semaphore` caps in-flight jobs at
   `WORKER_CONCURRENCY`. Async handlers run on the event loop; **sync/CPU-bound
   handlers are dispatched to a `ProcessPoolExecutor`** so the GIL never stalls
   the loop. Every handler is timeout-wrapped.
3. **Heartbeat loop** — writes `last_heartbeat_at` and a `worker_heartbeats`
   sample on a fixed cadence. Silence is the *only* signal the reaper trusts.

**Graceful shutdown:** the container `exec`s the process (via `entrypoint.sh`) so
`SIGTERM` reaches it directly. On `SIGTERM` the worker stops claiming, drains
in-flight jobs within `WORKER_SHUTDOWN_GRACE_S`, and **releases** any it could not
finish — releasing *refunds the attempt*, because a released job never ran. This
is the deliberate opposite of the reaper's lost-job path (§3). `stop_grace_period`
in compose is set above the drain budget so Docker never `SIGKILL`s mid-drain.

---

## 6. Deployment topology (docker-compose)

Eight containers by default:

- `postgres` (health-gated; every other service waits on it)
- `api` (the **single migration owner** — `RUN_MIGRATIONS=true`; no other
  process applies migrations, so none can race the `alembic_version` row)
- `worker` ×3 (no `container_name`, so `--scale worker=N` works)
- `scheduler` ×2 (`replicas: 2`; one wins the lock, one stands by)
- `web` (Next.js dashboard)

The image `exec`s its service so signals propagate; `.gitattributes` forces LF so
the entrypoint script runs on a Windows checkout. The dashboard calls the API
directly over CORS at the host-exposed port rather than through a proxy
(`DESIGN-DECISIONS.md §7`).

---

## 7. Request, data, and event flow

- **Write path (enqueue):** browser → `api` (validate, auth, RBAC, idempotency
  check) → `INSERT` into `jobs` → returns `202`-style job row. The job is now
  claimable by any worker.
- **Execution path:** `worker` claim loop → `CLAIM_SQL` (atomic) → executor runs
  the handler → `complete`/`retry`/`exhaust` write (fenced) → `job_executions` +
  `job_logs` rows.
- **Read path (dashboard):** browser → `api` read endpoints (keyset-paginated
  lists, `date_bin` metric aggregates) and one **SSE** stream (`GET /events`)
  that pushes a fleet+health snapshot every `interval` seconds. One `EventSource`
  is shared across all pages in `AppShell`.

See `API.md` for the full endpoint surface and `DESIGN-DECISIONS.md` for the
rationale behind each mechanism named here.
