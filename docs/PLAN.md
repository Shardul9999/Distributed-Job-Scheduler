# Distributed Job Scheduler — Implementation Plan

**Assignment:** Codity Intern Technical Assessment
**Deadline:** 25 August 2026
**Plan written:** 21 August 2026
**Stack:** Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2.0 (async) · Next.js

---

## 0. What we are building, in one paragraph

A job queue system: producers push work items into named queues over a REST API;
a fleet of independent worker processes competes to claim those items, executes
them concurrently, and records the outcome. Jobs can be immediate, delayed,
cron-recurring, or submitted in batches. Failures retry with configurable backoff
and permanently-failed jobs land in a Dead Letter Queue. A web dashboard observes
the whole system live. In short: **we are building Celery from scratch, on
PostgreSQL, and we must be able to explain every mechanism.**

---

## 1. The rubric drives every decision

| Area | Marks | Where it is earned |
|---|---|---|
| System Architecture | 20 | Process separation, layered modules, docker-compose topology |
| Database Design | 20 | 13-table schema, partial indexes, FK cascade policy, normalization notes |
| Backend Engineering | 20 | Service layer, validation, auth, pagination, error envelope, structured logs |
| Reliability & Concurrency | 15 | `SKIP LOCKED` claim, heartbeats + reaper, idempotency, graceful shutdown |
| Frontend & UX | 10 | Six dashboard pages, live updates |
| API Design | 5 | Consistent REST, OpenAPI, keyset pagination |
| Documentation | 5 | Architecture + ER diagrams, design-decisions doc |
| Testing | 5 | The 10-worker no-double-execution test |

**75 of 100 marks are backend, database, and architecture.** The frontend is worth
10 — it must look competent and load fast, and no more than that. The document's
closing line is explicit that quality beats feature count, so we implement the
core flawlessly and pick **three** bonus features, not eight.

---

## 2. Architecture — four process types, one database

```
                    ┌──────────────────────────────┐
                    │   Next.js dashboard (web)    │
                    └──────────────┬───────────────┘
                            REST + SSE
                                   │
                    ┌──────────────▼───────────────┐
                    │   FastAPI  (api)  xN         │  stateless, scales horizontally
                    │   auth · CRUD · enqueue · SSE│
                    └──────────────┬───────────────┘
                                   │
              ┌────────────────────▼────────────────────┐
              │          PostgreSQL 16                  │  <-- the queue IS the database
              │   jobs · queues · executions · workers  │
              └───▲──────────────▲──────────────▲───────┘
                  │              │              │
      ┌───────────┴───┐  ┌───────┴───────┐  ┌───┴────────────┐
      │ worker x3     │  │ scheduler x1  │  │ reaper         │
      │ claim-run-ack │  │ cron to jobs  │  │ revive orphans │
      │ heartbeat     │  │ advisory lock │  │ (in scheduler) │
      └───────────────┘  └───────────────┘  └────────────────┘
```

**Why four separate processes and not one app with background threads:**
this is the "distributed" requirement made literal. The API can be restarted
without dropping running jobs. Workers scale independently of request traffic.
A crashed worker is a survivable event, not an outage. This separation is the
single clearest signal for the 20 architecture marks.

**Why PostgreSQL as the queue and not Redis / RabbitMQ / Celery:**
the assignment grades atomic claiming, retry policy, delayed execution and
dead-lettering — precisely the features a queue library would provide for us.
Using BullMQ or Celery would mean importing the answer to the exam. Postgres
`SKIP LOCKED` lets us build it ourselves in ~15 lines of SQL, gives transactional
consistency between "job claimed" and "execution row written", and is what real
systems do (GitLab, Oban, River, graphile-worker). Redis appears only for the
rate-limiting bonus, never as the job store.

**Runtime note:** all services run on `python:3.12-slim` inside Docker. Local
Python 3.14 is not used — `asyncpg` wheels lag new interpreter releases and we
are not spending deadline hours on a build toolchain.

---

## 3. The four hard problems, and how each is solved

### Problem 1 — Two workers must never run the same job

```sql
WITH claimable AS (
    SELECT j.id
    FROM jobs j
    JOIN queues q ON q.id = j.queue_id
    WHERE j.status IN ('queued', 'scheduled')
      AND j.run_at <= now()
      AND q.is_paused = false
      AND j.queue_id = ANY(:queue_ids)
    ORDER BY j.priority DESC, j.run_at ASC
    FOR UPDATE OF j SKIP LOCKED
    LIMIT :batch_size
)
UPDATE jobs
SET status      = 'claimed',
    claimed_by  = :worker_id,
    claimed_at  = now(),
    lock_token  = gen_random_uuid(),
    attempt     = attempt + 1
FROM claimable
WHERE jobs.id = claimable.id
RETURNING jobs.*;
```

`FOR UPDATE` row-locks the candidates. `SKIP LOCKED` makes a competing worker
step *over* rows another transaction already holds instead of blocking on them —
so ten workers claim ten disjoint batches with zero contention and zero waiting.
`FOR UPDATE OF j` is required because the join means we must name which table to
lock. The whole statement is one round trip: select, lock, and mark claimed
atomically, so a crash mid-claim rolls back cleanly.

`lock_token` is issued at claim time and must be presented on completion. If a
job is reaped and re-run while a zombie worker is still alive, the zombie's
`UPDATE ... WHERE lock_token = :token` matches zero rows and its stale result is
discarded. This is fencing, and it is what makes the reaper safe.

### Problem 2 — A worker dies mid-job and the job is stranded in RUNNING

Every worker writes `last_heartbeat_at` every 10 seconds. The **reaper** runs
every 30 seconds and does two things:

1. Marks workers with no heartbeat in 60s as `dead`.
2. Requeues their in-flight jobs, writing a `job_executions` row with status
   `lost` so the failure is visible in history rather than silent.

Requeued jobs respect the retry policy — a job lost three times to crashing
workers eventually dead-letters like any other failure.

### Problem 3 — Retries, backoff, and the Dead Letter Queue

On failure, compute the next attempt time from the queue's retry policy:

| Strategy | Delay for attempt *n* |
|---|---|
| `fixed` | `base_delay` |
| `linear` | `base_delay x n` |
| `exponential` | `base_delay x 2^(n-1)`, capped at `max_delay` |

Full jitter (`delay x random()`) is applied optionally to prevent thundering-herd
retry storms — a small detail that reads as production experience.

When `attempt >= max_attempts` the job moves to `dead` and a `dead_letter_queue`
row is written capturing the original payload, total attempts, and final error.
DLQ entries are replayable from the dashboard: replay inserts a *new* job and
links it via `replayed_job_id`, preserving the forensic record instead of
mutating it.

### Problem 4 — Time-based jobs

| Kind | Representation |
|---|---|
| Immediate | `status='queued'`, `run_at = now()` |
| Delayed | `status='scheduled'`, `run_at = now() + delay` |
| Scheduled | `status='scheduled'`, `run_at = <timestamp>` |
| Batch | N rows sharing a `batch_id`, inserted in one transaction |
| Recurring | a `scheduled_jobs` row holding a cron expression |

A recurring entry is a **template, not a job**. The scheduler process wakes every
second, finds templates where `next_run_at <= now()`, inserts a concrete `jobs`
row for each, and advances `next_run_at` using `croniter` in the template's
timezone. The scheduler holds a Postgres advisory lock (`pg_try_advisory_lock`)
so running multiple replicas is safe but only one ever materializes jobs —
**this also satisfies the "distributed locking" bonus for free.**

---

## 4. Database schema — 13 tables

### Identity and tenancy

**`users`** — `id` uuid PK · `email` citext UNIQUE · `password_hash` · `full_name`
· `is_active` · `created_at` · `updated_at`

**`organizations`** — `id` uuid PK · `name` · `slug` UNIQUE · `created_at`

**`organization_members`** — `org_id` FK→organizations ON DELETE CASCADE ·
`user_id` FK→users ON DELETE CASCADE · `role` enum(`owner`,`admin`,`member`,`viewer`)
· PK(`org_id`,`user_id`)
*Junction table resolving the many-to-many between users and orgs. Carrying
`role` here rather than on `users` makes RBAC a per-organization concept — the
whole RBAC bonus becomes one dependency check.*

**`projects`** — `id` uuid PK · `org_id` FK CASCADE · `name` · `slug` ·
`api_key_hash` · `created_at` · UNIQUE(`org_id`,`slug`)

### Queue configuration

**`retry_policies`** — `id` uuid PK · `project_id` FK CASCADE · `name` ·
`strategy` enum(`fixed`,`linear`,`exponential`) · `max_attempts` ·
`base_delay_ms` · `max_delay_ms` · `jitter` bool
*Extracted into its own table rather than inlined on `queues` so one policy can
be reused across queues — this is the normalization decision to defend in the
design doc.*

**`queues`** — `id` uuid PK · `project_id` FK CASCADE · `name` · `priority` int ·
`max_concurrency` int · `is_paused` bool · `retry_policy_id` FK ON DELETE RESTRICT
· `rate_limit_per_sec` int null · `visibility_timeout_s` · `created_at` ·
UNIQUE(`project_id`,`name`)
*`RESTRICT` on the policy FK is deliberate: deleting a policy that live queues
depend on must fail loudly, not silently null out retry behaviour.*

### The job tables

**`jobs`** — the hot table.
`id` uuid PK · `queue_id` FK CASCADE · `job_type` text · `payload` jsonb ·
`status` enum(`queued`,`scheduled`,`claimed`,`running`,`completed`,`failed`,`dead`,`cancelled`)
· `priority` int · `attempt` int · `max_attempts` int · `run_at` timestamptz ·
`claimed_at` · `started_at` · `completed_at` · `claimed_by` FK→workers ON DELETE SET NULL
· `lock_token` uuid · `idempotency_key` text null · `batch_id` uuid null ·
`scheduled_job_id` FK→scheduled_jobs ON DELETE SET NULL · `depends_on` uuid[] null ·
`last_error` text · `result` jsonb · `timeout_s` · `created_at` · `updated_at`

**`job_executions`** — one row per *attempt*, never overwritten.
`id` uuid PK · `job_id` FK CASCADE · `attempt_number` · `worker_id` FK SET NULL ·
`started_at` · `finished_at` · `duration_ms` ·
`status` enum(`succeeded`,`failed`,`timeout`,`lost`) · `error_message` ·
`error_stack` · `output` jsonb
*Separating attempts from jobs is the core normalization call: `jobs` holds
current state, `job_executions` holds immutable history. Without this split,
"retry history" would be an unqueryable JSON blob.*

**`job_logs`** — `id` bigserial PK · `job_id` FK CASCADE · `execution_id` FK CASCADE
· `level` enum · `message` · `metadata` jsonb · `logged_at`
*bigserial not uuid — this is the highest-volume table and a monotonic key keeps
inserts append-only and the index dense.*

**`dead_letter_queue`** — `id` uuid PK · `job_id` FK · `queue_id` FK CASCADE ·
`original_payload` jsonb · `failure_reason` · `total_attempts` · `died_at` ·
`replayed_at` null · `replayed_job_id` FK null

**`scheduled_jobs`** — `id` uuid PK · `queue_id` FK CASCADE · `name` ·
`cron_expression` · `timezone` · `job_type` · `payload` jsonb · `is_active` ·
`last_run_at` · `next_run_at` · `created_at`

### Worker fleet

**`workers`** — `id` uuid PK · `project_id` FK null · `hostname` · `pid` ·
`version` · `concurrency` · `queue_names` text[] ·
`status` enum(`starting`,`active`,`draining`,`dead`,`stopped`) · `started_at` ·
`last_heartbeat_at` · `stopped_at`

**`worker_heartbeats`** — `id` bigserial PK · `worker_id` FK CASCADE · `beat_at` ·
`active_jobs` · `jobs_processed` · `cpu_percent` · `memory_mb`
*A time series kept separate from `workers` so the current-state row stays small
and hot while history grows. Trimmed to 24h by a maintenance task; the design doc
notes `pg_partman` monthly partitioning as the scale-out path.*

### Indexes — the part that earns the marks

```sql
-- THE index. Partial: only rows a worker could claim are in it.
-- A table with 10M completed jobs keeps an index holding only the few
-- thousand pending ones, so claim latency stays flat as history grows.
CREATE INDEX idx_jobs_claim ON jobs (queue_id, priority DESC, run_at ASC)
    WHERE status IN ('queued', 'scheduled');

CREATE INDEX idx_jobs_reaper ON jobs (claimed_at)
    WHERE status IN ('claimed', 'running');

CREATE UNIQUE INDEX idx_jobs_idempotency ON jobs (queue_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX idx_jobs_batch    ON jobs (batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_jobs_explorer ON jobs (queue_id, status, created_at DESC);

CREATE INDEX idx_exec_job      ON job_executions (job_id, attempt_number DESC);
CREATE INDEX idx_logs_job      ON job_logs (job_id, logged_at DESC);
CREATE INDEX idx_workers_alive ON workers (last_heartbeat_at) WHERE status = 'active';
CREATE INDEX idx_sched_due     ON scheduled_jobs (next_run_at) WHERE is_active;
CREATE INDEX idx_hb_worker     ON worker_heartbeats (worker_id, beat_at DESC);
```

Every index above exists to serve one named query. That mapping — index to query —
is what we write in the design-decisions document.

---

## 5. Job lifecycle state machine

```
                    ┌──────────┐
   run_at future -> │ SCHEDULED│
                    └────┬─────┘
                         │ run_at <= now()
   run_at now    -> ┌────▼─────┐
                    │  QUEUED  │◄──────────────────┐
                    └────┬─────┘                   │
                         │ SKIP LOCKED claim       │ retry scheduled
                    ┌────▼─────┐                   │ (backoff delay)
                    │ CLAIMED  │                   │
                    └────┬─────┘                   │
                         │ executor starts         │
                    ┌────▼─────┐                   │
                    │ RUNNING  │───── error ──►┌───┴────┐
                    └────┬─────┘               │ FAILED │
                         │ ok                  └───┬────┘
                    ┌────▼─────┐                   │ attempt >= max
                    │COMPLETED │              ┌────▼───┐
                    └──────────┘              │  DEAD  │──► dead_letter_queue
                                              └────────┘

   CANCELLED reachable from SCHEDULED / QUEUED only.
   Reaper moves stranded CLAIMED/RUNNING back to QUEUED as attempt `lost`.
```

The assignment lists `Queued → Scheduled` in that order; we model both states and
the claim predicate `status IN ('queued','scheduled') AND run_at <= now()`
handles either interpretation. This ambiguity and our reading of it goes in the
design-decisions document.

---

## 6. Worker internals — where "concurrency" is demonstrated

One worker process, one asyncio event loop, three concurrent tasks:

```python
async def run():
    register_worker()
    await asyncio.gather(
        heartbeat_loop(),    # every 10s: UPDATE workers SET last_heartbeat_at
        claim_loop(),        # claim batch -> spawn executors -> repeat
        shutdown_watcher(),  # SIGTERM/SIGINT -> drain
    )
```

- **Concurrency cap:** `asyncio.Semaphore(worker_concurrency)`; each claimed job
  is dispatched with `asyncio.create_task` and acquires the semaphore.
- **Batch sizing:** claim `min(free_slots, queue.max_concurrency - running)` —
  never claim work we cannot start, so jobs stay available to idle workers.
- **Queue-level concurrency** (a cap across the *whole fleet*, not per worker) is
  enforced inside the claim CTE with a lateral count of running jobs per queue.
  Trade-off noted in the design doc: correct and simple, but a per-claim count;
  a Redis counter is the scale-out path.
- **Timeouts:** every job runs under `asyncio.wait_for(..., job.timeout_s)`;
  expiry records execution status `timeout` and follows the retry policy.
- **CPU-bound job types** are dispatched to a `ProcessPoolExecutor` via
  `loop.run_in_executor`, sidestepping the GIL entirely. Documented as an
  explicit design decision.
- **Graceful shutdown:** SIGTERM -> status `draining` -> stop claiming ->
  `await asyncio.gather(*in_flight)` under a 30s grace period -> any stragglers
  released back to `queued` -> mark worker `stopped`. Docker's default 10s
  SIGKILL delay is raised in compose so drain actually completes.

**Backoff when idle:** an empty claim sleeps with exponential backoff from 100ms
to 2s, so an idle fleet does not hammer the database — and `LISTEN/NOTIFY` on
insert is documented as the event-driven upgrade path.

---

## 7. API surface

Base `/api/v1`. Envelope on every error:
`{"error": {"code", "message", "details", "request_id"}}`

| Group | Endpoints |
|---|---|
| Auth | `POST /auth/register` · `POST /auth/login` · `POST /auth/refresh` · `GET /auth/me` |
| Orgs | `GET,POST /orgs` · `GET,PATCH,DELETE /orgs/{id}` · `GET,POST,DELETE /orgs/{id}/members` |
| Projects | `GET,POST /projects` · `GET,PATCH,DELETE /projects/{id}` |
| Queues | `GET,POST /projects/{id}/queues` · `GET,PATCH,DELETE /queues/{id}` · `POST /queues/{id}/pause` · `POST /queues/{id}/resume` · `GET /queues/{id}/stats` |
| Policies | `GET,POST /projects/{id}/retry-policies` · `PATCH,DELETE /retry-policies/{id}` |
| Jobs | `POST /queues/{id}/jobs` · `POST /queues/{id}/jobs/batch` · `GET /jobs` (filter + keyset page) · `GET /jobs/{id}` · `GET /jobs/{id}/executions` · `GET /jobs/{id}/logs` · `POST /jobs/{id}/retry` · `POST /jobs/{id}/cancel` |
| Schedules | `GET,POST /queues/{id}/schedules` · `PATCH,DELETE /schedules/{id}` · `POST /schedules/{id}/trigger` |
| Workers | `GET /workers` · `GET /workers/{id}` · `GET /workers/{id}/heartbeats` |
| DLQ | `GET /projects/{id}/dlq` · `POST /dlq/{id}/replay` · `DELETE /dlq/{id}` |
| Metrics | `GET /metrics/throughput` · `GET /metrics/health` · `GET /metrics/latency` |
| Live | `GET /events` (SSE) |

**Keyset pagination**, not `OFFSET`: `?cursor=<created_at,id>&limit=50`.
On a jobs table with millions of rows `OFFSET 100000` scans and discards 100k
rows; keyset seeks the index directly. Worth one paragraph in the design doc and
exactly what an API-design mark looks for.

**Idempotency:** `POST /queues/{id}/jobs` accepts an `Idempotency-Key` header;
a duplicate key returns the original job with `200` instead of creating a second.

---

## 8. Frontend — six pages, timeboxed to one day

Next.js App Router · Tailwind · shadcn/ui · TanStack Query · Recharts.

1. **Overview** — throughput chart, queue-depth chart, live worker count, failure rate
2. **Queues** — table with depth/rate/paused, config drawer, pause/resume toggle
3. **Job Explorer** — filterable, paginated table; row opens a detail drawer with
   payload, execution timeline, logs, retry/cancel
4. **Workers** — fleet table, heartbeat freshness indicator, active-job counts
5. **Schedules** — cron entries, next-fire time, active toggle, manual trigger
6. **DLQ** — failed jobs, error inspection, replay button

Live updates via **SSE**, not WebSockets. `EventSource` is ~20 lines client-side,
survives reconnects natively, and the data flow here is strictly server-to-client
so a bidirectional socket buys nothing. Documented as a deliberate trade-off.

---

## 9. Repository layout

```
Codity/
├── docker-compose.yml          # postgres, api, worker x3, scheduler, web, redis
├── README.md                   # setup instructions (deliverable)
├── .env.example
├── apps/
│   ├── api/                    # FastAPI
│   │   ├── main.py
│   │   ├── core/               # config, security, deps, errors, logging
│   │   ├── routers/            # one module per API group
│   │   ├── schemas/            # Pydantic v2 request/response models
│   │   └── services/           # business logic — routers stay thin
│   ├── worker/
│   │   ├── main.py             # claim loop, heartbeat, shutdown
│   │   ├── claimer.py          # THE SQL
│   │   ├── executor.py         # semaphore, timeout, result recording
│   │   ├── retry.py            # backoff strategies
│   │   └── handlers/           # job type implementations
│   ├── scheduler/
│   │   ├── main.py             # advisory lock, tick loop
│   │   ├── cron.py             # croniter -> next_run_at
│   │   └── reaper.py           # dead worker detection, orphan recovery
│   └── web/                    # Next.js
├── packages/
│   └── db/
│       ├── models.py           # SQLAlchemy 2.0 declarative
│       ├── session.py
│       └── migrations/         # Alembic
├── tests/
│   ├── test_claim_concurrency.py   # the 10-worker test
│   ├── test_retry_backoff.py
│   ├── test_reaper.py
│   ├── test_scheduler_cron.py
│   └── test_api_*.py
└── docs/
    ├── PLAN.md                 # this file
    ├── ARCHITECTURE.md         # + mermaid diagram
    ├── ER-DIAGRAM.md           # mermaid erDiagram
    ├── DESIGN-DECISIONS.md     # trade-offs (deliverable)
    └── API.md                  # + auto OpenAPI at /docs
```

---

## 10. Testing — the tests that actually matter

**`test_claim_concurrency.py` is the single highest-value test in the project.**
Insert 500 jobs, launch 10 concurrent claim loops against a real Postgres
(Testcontainers), let them drain the queue, then assert:

- every job has exactly one `succeeded` execution row
- `count(distinct job_id) == 500`
- no job was claimed by two workers

This one test proves the concurrency story more convincingly than three pages of
prose, and it targets the 15-mark reliability section directly.

Supporting tests: backoff delay maths per strategy; reaper requeues a job whose
worker stopped heartbeating; cron `next_run_at` advances correctly across a DST
boundary; DLQ entry created on final failure; idempotency key returns the
original job; auth rejects cross-organization access.

---

## 11. Bonus features — three, chosen for cost/impact ratio

| Feature | Cost | Why this one |
|---|---|---|
| **Distributed locking** | ~0 | Already required for the singleton scheduler — `pg_try_advisory_lock`. Free mark. |
| **RBAC** | ~1h | `organization_members.role` is already in the schema; one FastAPI dependency enforces it. |
| **AI failure summaries** | ~1h | Claude API summarises a stack trace into a human-readable cause on the job detail page. Highest visible impact per hour on the whole list. |

Deferred and explicitly named in the design doc as *known, scoped out*: workflow
DAG dependencies (the `depends_on` column is already reserved), queue sharding,
rate limiting. Naming what we chose not to build — and why — reads as engineering
judgement, not omission.

---

## 12. Build schedule — four days

| When | Deliverable | Gate |
|---|---|---|
| **Aug 21 evening** | Repo scaffold, docker-compose, full SQLAlchemy models, Alembic migration, auth (register/login/JWT), org + project CRUD | `docker compose up` boots; can register and create a project |
| **Aug 22** | Queue + retry-policy CRUD, job creation (immediate/delayed/scheduled/batch), **the claim query**, worker skeleton, executions + logs | A worker drains a queue end-to-end |
| **Aug 23** | Retry strategies, DLQ, cron scheduler + advisory lock, reaper, heartbeats, graceful shutdown, **concurrency test**, metrics endpoints | 10-worker test green; `kill -9` a worker and watch the job recover |
| **Aug 24** | All six dashboard pages, SSE live updates, charts | Dashboard drives the whole system |
| **Aug 25 AM** | ARCHITECTURE.md, ER-DIAGRAM.md, DESIGN-DECISIONS.md, README, OpenAPI export, seed script, remaining tests, three bonuses | Submit |

**Hard rule:** if Aug 23 slips, the frontend is cut to four pages before any core
reliability work is dropped. Frontend is 10 marks; reliability is 15 and
architecture is 20.

---

## 13. Design decisions to write up (each is worth marks)

1. Postgres-as-queue vs Redis/Celery — why not importing the answer
2. `SKIP LOCKED` over advisory locks or plain `SELECT ... FOR UPDATE` — contention behaviour
3. Fencing tokens — why `lock_token` makes reaping safe
4. `jobs` vs `job_executions` — current state vs immutable history
5. Partial indexes — keeping claim latency flat as history grows
6. Keyset vs offset pagination
7. SSE over WebSockets for a unidirectional feed
8. FK cascade policy — CASCADE for ownership, RESTRICT for referenced config,
   SET NULL for observability links
9. asyncio + multi-process over threads — and why the GIL is irrelevant to a
   multi-process architecture
10. Polling with backoff now, `LISTEN/NOTIFY` as the event-driven upgrade path
