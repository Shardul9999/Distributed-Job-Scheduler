# Project State / Session Handoff

**Read this first when resuming work.** It records exactly where the build is,
what is verified, and what comes next. Updated at the end of each day.

Last updated: **22 Aug 2026 — Day 2 COMPLETE**

---

## Fast resume

```bash
cd E:\Codity
docker compose up -d
curl http://localhost:8000/health
```

Repo: https://github.com/Shardul9999/Distributed-Job-Scheduler (`main` tracks `origin/main`)
Deadline: **25 August 2026**. Plan: [PLAN.md](PLAN.md). Rubric weighting drives all scope calls.

Commits carry **Shardul's authorship only** — never add a Claude co-author trailer.

---

## Settled decisions (do not relitigate)

| Decision | Rationale |
|---|---|
| **PostgreSQL is the queue** | Rubric grades atomic claiming/retries/DLQ; a queue library would import the answer |
| **Python + FastAPI**, not Go | Claiming is a Postgres `SKIP LOCKED` problem; multi-process architecture makes the GIL irrelevant |
| **asyncpg + SQLAlchemy 2.0 async** | One driver across API, worker, scheduler and Alembic |
| **Keyset pagination**, not OFFSET | Constant cost at depth on a multi-million-row `jobs` table |
| **SSE**, not WebSockets | Data flow is server→client only |
| **`depends_on` column kept** | Explicitly requested; reserved for workflow DAGs, not implemented |
| **Bonuses capped at 3** | Distributed locking, RBAC, AI failure summaries |
| Python 3.12 in Docker | Local 3.14 unused; asyncpg wheels lag new interpreters |

---

## Day 0 — COMPLETE, committed `a0710c4`, pushed

**Database** (`packages/db/`) — all 13 tables live and migrated.

Critical index, verified in the running database:
```sql
CREATE INDEX idx_jobs_claim ON jobs (queue_id, priority DESC, run_at)
  WHERE (status = ANY (ARRAY['queued'::job_status, 'scheduled'::job_status]))
```
Its predicate must stay in sync with `CLAIMABLE_STATUSES` in `packages/db/enums.py`,
or the claim query silently falls back to a sequential scan.

Also present: `idx_jobs_reaper`, `idx_jobs_idempotency` (partial unique),
`idx_jobs_batch`, `idx_jobs_explorer`, `idx_sched_due`, `idx_workers_alive`,
`idx_dlq_unreplayed`. Native PG enums for every state machine.

Reserved-but-unused columns: `jobs.lock_token` (fencing token, used Day 2),
`jobs.depends_on` (DAGs, not implemented).

**API** (`apps/api/`) — 21 endpoints. JWT access/refresh with type-checked
decode, Argon2id hashing, org/project CRUD with RBAC, uniform error envelope
with `request_id`, structured logging, keyset pagination primitives, split
liveness/readiness probes.

**Infra** — one Docker image for all process types; compose with health-gated
ordering; entrypoint `exec`s the service so SIGTERM reaches it directly
(required for Day 2 graceful shutdown); `.gitattributes` forces LF.

**Verified end-to-end:** register → login → `/auth/me` → create project → list;
plus 409 / 401 / 422 / 404 paths and cross-tenant isolation (404 not 403, so
org ids cannot be enumerated).

---

## Day 1 — COMPLETE, committed, pushed

**The claim query** lives in `packages/db/claim.py` (`CLAIM_SQL`). Kept in
`packages/db` so the API, the worker and the tests all execute the *same*
statement -- a concurrency test against a reimplementation proves nothing.

Structure: `headroom` CTE (fleet-wide `max_concurrency` minus in-flight; yields
no rows when the queue is paused, which is how pause/resume is enforced) ->
`claimable` (`FOR UPDATE OF j SKIP LOCKED`, ordered to match `idx_jobs_claim`)
-> `UPDATE` stamping owner, `claimed_at`, a fresh `lock_token`, and `attempt+1`.

Claims are **per queue**, looped in the worker. A single cross-queue statement
would need a window function to apply each queue's cap, and PostgreSQL forbids
`FOR UPDATE` alongside window functions. Documented as a trade-off.

Every result write (`complete`/`retry`/`exhaust`) is fenced by `lock_token`.
A stale worker's write matches zero rows and is discarded rather than
overwriting the live attempt.

**Measured** (50,205 rows, 202 claimable):

| | |
|---|---|
| `idx_jobs_claim` (partial) | **16 kB** |
| `idx_jobs_explorer` (full) | 344 kB |
| Plan | `Index Scan using idx_jobs_claim`, **no Sort node**, 4 buffer hits, 0.043 ms |

That 21x figure belongs in DESIGN-DECISIONS.md.

**Built:** retry-policy CRUD; queue CRUD + pause/resume + stats; job enqueue
(immediate/delayed/scheduled/batch, `Idempotency-Key` header); job explorer with
keyset pagination + filters; job detail/executions/logs; manual retry + cancel.
API now serves **41 endpoints**.

**Worker** (`apps/worker/`): `main.py` (claim loop with idle backoff 100ms->2s,
heartbeat loop, SIGTERM drain), `executor.py` (semaphore-capped, timeout-wrapped,
sync handlers dispatched to a ProcessPoolExecutor to sidestep the GIL),
`retry.py` (fixed/linear/exponential + full jitter), `handlers.py`
(echo, sleep, http_get, fail, cpu_burn).

`worker` added to compose with `stop_grace_period: 45s` (must exceed
`WORKER_SHUTDOWN_GRACE_S=30`) and no `container_name`, so `--scale worker=N`
works.

**Verified end-to-end:**
- 20 batch jobs, 2 workers -> 20 executions, 20 distinct, **0 run twice**
- fail-twice-then-succeed -> 3 immutable execution rows, correct backoff
- SIGTERM with 5 in flight -> `finished=5, abandoned=0`, clean `stopped`
- 27 jobs total, all completed, **0 duplicate attempts**

*(The Day 1 DLQ gap is closed -- see Day 2 below.)*

---

## Day 2 — COMPLETE, committed, pushed

**Fourth process type.** `apps/scheduler/` is a leader-elected singleton running
two fleet-wide jobs that must happen exactly once: cron materialisation and
crash recovery. Two replicas run in compose; only one acts.

**Leader election** (`main.py`): `pg_try_advisory_lock(SCHEDULER_LOCK_KEY)` on a
*dedicated* connection held for the process lifetime. The lock is session-scoped,
so it dies with the connection -- no lease, no TTL, no split-brain window. A
loser logs `scheduler.standby` and retries every `SCHEDULER_LOCK_RETRY_S`. The
key lives in `packages/locks.py` because the API also reads `pg_locks` to report
whether a leader exists. **This is the distributed-locking bonus, banked.**

**Reaper** (`reaper.py`), three sweeps, in this order:
1. workers silent past `WORKER_HEARTBEAT_TIMEOUT_S` (60s) -> `dead`
2. jobs held by a dead/stopped worker, **or** whose claim outlived the queue's
   `visibility_timeout_s` -> `lost` execution row, then requeue with backoff or
   dead-letter. `lock_token = NULL` fences the zombie out.
3. `worker_heartbeats` trimmed in bounded batches

Recovery does **not** refund the attempt -- the deliberate opposite of graceful
shutdown's `release_jobs`. A released job never ran; a lost job may have had
side effects, so it consumes a retry and eventually dead-letters.

**Cron** (`cron.py`): croniter evaluated in the schedule's IANA timezone, so a
schedule survives DST. Missed occurrences are **skipped, not backfilled** (an
hour of downtime on `* * * * *` yields one job, not sixty). Each occurrence is
keyed `cron:<schedule id>:<fire time>` into `idx_jobs_idempotency`, so a crash
between the insert and the `next_run_at` advance cannot double-fire.

**DLQ gap closed:** `EXHAUST_SQL` is now one CTE statement that flips the job to
`dead` and inserts the `dead_letter_queue` row together. A stale token produces
an empty `dead` arm, so the INSERT selects from nothing -- a zombie cannot
dead-letter a live job.

**API** now serves **54 endpoints** (+13): schedules CRUD + manual trigger, DLQ
list/detail/replay/discard, `/workers`, `/workers/{id}`, `/fleet/stats`.
`fleet_stats.scheduler_leader_present` reads `pg_locks` directly -- no
self-reporting, so a crashed scheduler cannot look healthy.

**Moved:** `apps/worker/retry.py` -> `packages/retry.py`. Worker and reaper must
compute identical backoff; a job lost to a dead machine should not retry on a
different schedule from one that failed in code.

**Test suite** (`tests/`, pytest + testcontainers): **32 tests, all passing.**
Real PostgreSQL, schema built by `alembic upgrade head` (not `create_all` --
the migration is what ships). `TEST_DATABASE_URL` points the suite at an
existing database, which is how it runs inside the API container.

```bash
docker compose exec -T -e TEST_DATABASE_URL="postgresql+asyncpg://codity:codity_dev_password@postgres:5432/codity_test"   api python -m pytest tests/ -q          # 32 passed
```

**Verified live, at scale:**

| Check | Result |
|---|---|
| 10 workers, 500 jobs, concurrent claims | 500 claimed, **0 duplicates**, all at attempt 1 |
| `max_concurrency=3` vs 10 workers | 3 claims fleet-wide, not 3 per worker |
| **`kill -9` gate:** 10 containers, 300 x 8s jobs, killed the worker holding 10 | 300/300 completed · 300 succeeded executions · 10 `lost` · **0 jobs succeeded twice** · 0 duplicate (job, attempt) pairs |
| Scheduler failover: `kill -9` the leader | standby acquired in **5s**, unattended |
| Cron `* * * * *` in `Asia/Kolkata` | fired on the minute, one job per occurrence |
| DLQ round trip | exhaust -> entry with payload + stack -> replay -> new job, second replay 409 |

## Remaining days

| Day | Scope | Gate |
|---|---|---|
| 3 (Aug 24) | Six dashboard pages, SSE, charts | Dashboard drives the system |
| 4 (Aug 25 AM) | ARCHITECTURE.md, ER-DIAGRAM.md, DESIGN-DECISIONS.md, API docs, bonuses | Submit |

**Hard rule:** cut frontend scope before cutting reliability work. Frontend is
10 marks; reliability is 15 and architecture is 20.

---

## Design decisions still owed to DESIGN-DECISIONS.md

Postgres-as-queue vs Celery · `SKIP LOCKED` vs advisory locks · fencing tokens ·
`jobs` vs `job_executions` · partial indexes (the 21x figure) · keyset vs offset ·
SSE vs WebSockets · FK cascade policy · asyncio multi-process vs threads (GIL) ·
polling vs `LISTEN/NOTIFY` · native enums vs CHECK constraints · citext vs
normalized email.

Added Day 2: advisory-lock leader election vs etcd/Redis · why the lock needs a
dedicated connection · lost-attempt consumes a retry while a released one does
not · cron skip-vs-backfill · occurrence idempotency keys · per-schedule IANA
timezone vs stored UTC offset · `pg_locks` as liveness vs a scheduler heartbeat ·
heartbeat retention by batched DELETE, with pg_partman as the scale-out path.
