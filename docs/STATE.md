# Project State / Session Handoff

**Read this first when resuming work.** It records exactly where the build is,
what is verified, and what comes next. Updated at the end of each day.

Last updated: **21 Aug 2026 — Day 1 COMPLETE**

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

**Known gap (Day 2):** `exhaust_job` marks a job `dead` but does not yet write
the `dead_letter_queue` row. Pair them in one transaction.

## Remaining days

| Day | Scope | Gate |
|---|---|---|
| 2 (Aug 23) | Retry strategies, DLQ, cron scheduler + advisory lock, reaper, heartbeats, graceful shutdown, **10-worker concurrency test** | `kill -9` a worker, job recovers |
| 3 (Aug 24) | Six dashboard pages, SSE, charts | Dashboard drives the system |
| 4 (Aug 25 AM) | ARCHITECTURE.md, ER-DIAGRAM.md, DESIGN-DECISIONS.md, API docs, bonuses | Submit |

**Hard rule:** if Day 2 slips, cut frontend scope before cutting reliability work.
Frontend is 10 marks; reliability is 15 and architecture is 20.

---

## Design decisions still owed to DESIGN-DECISIONS.md

Postgres-as-queue vs Celery · `SKIP LOCKED` vs advisory locks · fencing tokens ·
`jobs` vs `job_executions` · partial indexes · keyset vs offset · SSE vs
WebSockets · FK cascade policy · asyncio multi-process vs threads (GIL) ·
polling vs `LISTEN/NOTIFY` · native enums vs CHECK constraints · citext vs
normalized email.
