# Project State / Session Handoff

**Read this first when resuming work.** It records exactly where the build is,
what is verified, and what comes next. Updated at the end of each day.

Last updated: **22 Aug 2026 — Day 4 COMPLETE (ready to submit)**

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

## Day 3 — COMPLETE, committed, pushed

**Backend first — the charts and live feed needed endpoints that did not exist.**

*Metrics* (`apps/api/routers/metrics.py`, `services/metrics_service.py`): three
global read-only aggregates — `/metrics/throughput` (executions per bucket by
outcome), `/metrics/latency` (percentile_cont p50/p95/p99 + bucketed line over
succeeded rows only), `/metrics/health` (recent success/failure + queue-depth by
status). Time series use PostgreSQL `date_bin` anchored at the epoch so buckets
don't jitter between refreshes, and are **gap-filled in Python** so an idle
bucket is a zero, not a hole. Served by the `idx_exec_metrics` index built Day 0.

*SSE* (`apps/api/routers/events.py`): `GET /events` streams a whole-system
snapshot (fleet + 5-min health) every `interval` seconds via `StreamingResponse`.
`EventSource` can't set headers, so the access token rides in `?token=` and is
validated exactly as the header form. A short session per tick — a dashboard left
open overnight must not pin a pooled connection.

**Frontend** — `apps/web/`, Next.js 15 App Router · Tailwind · TanStack Query ·
Recharts. Six pages: Overview (SSE tiles + throughput/depth/latency charts),
Queues (stats + pause/resume), Job Explorer (filter, keyset paging, detail
drawer with executions/logs/retry/cancel), Workers (fleet + heartbeat
freshness), Schedules (cron + trigger/toggle), Dead Letters (inspect + replay,
renders `ai_summary` when present). One shared `EventSource` in `AppShell`;
project switcher in the sidebar (register creates an org but no project, so the
switcher + empty states matter). shadcn/ui was planned but its interactive CLI
init doesn't belong in a one-command repo — primitives are hand-rolled in
`components/ui.tsx`. `npm run build` is clean: 10 routes, no type errors.

**Compose:** `web` service (`apps/web/Dockerfile`, node:24-alpine, `next dev`).
Host `node_modules` are Windows-native, so the container keeps its own via
anonymous volumes on `/app/web/node_modules` and `/app/web/.next`.
`NEXT_PUBLIC_API_BASE` points the browser at the host API port (CORS already
allows `:3000`).

**Seed script** (`scripts/seed.py`, stdlib only, hits the API): stands up
`demo@codity.dev` / `demodemo123` → project `Production` → 3 queues → a
`* * * * *` schedule → 71 jobs (echo, sleep, fail-then-recover, always-fail).
Re-runnable. Nominally a Day 4 item, built now because the Day 3 gate is
"dashboard drives the system" and it needs data to drive.

**Verified live:**

| Check | Result |
|---|---|
| `npm run build` | 10 routes, 0 type errors |
| web + api + 3 workers + 2 schedulers up | dashboard serves 200; SSE emits snapshots every 2s |
| seed → drain | 67 completed (40 echo + 18 sleep + 8 recovered + 1 cron), 5 dead |
| DLQ | 5 entries, `RuntimeError: Recipient address rejected`, replayable |
| cron `minute-heartbeat` | fired on the minute, `last_run` advances |
| throughput / latency | 30 non-empty buckets; 97 samples, p50 20ms / p95 1.9s |
| SSE auth | bad/missing token → 401/422 |

## Day 4 — COMPLETE, committed, pushed

**The final day: documentation, the third bonus, and a flaky-test fix.**

**Third bonus — AI failure summaries — finished.** Days 0–3 left the `ai_summary`
column and the DLQ page's render-when-present, but nothing generated it. Built
`apps/api/services/ai_summary.py`: **provider-agnostic over `httpx`** (already a
dep — no SDK), auto-detecting Groq (OpenAI-compatible) or Gemini from whichever
API key is set. Wired **lazily** into `dlq_service.get_entry` — generated on first
inspection of a dead letter, then persisted, so it is computed at most once and
never on the worker's failure path. **Best-effort:** no key / timeout / bad
response → `None`, logged, never raised. With no key the feature is inert and the
system runs and grades exactly as before. Config in `core/config.py`
(`active_ai_provider` resolves the effective provider or None); env passthrough
added to the `api` service in compose and documented in `.env.example`.
**User has no Anthropic key** — chose Groq/Gemini free tier; keys not committed.

**Flaky cron test fixed — and a second isolation bug found and fixed.** The known
`test_due_template_produces_exactly_one_job` flake was a global `materialize_due()`
count asserted `== 1`; scoped to `>= 1` (the per-schedule `len(jobs) == 1` already
proves exactly-once). Running the *full* suite then surfaced 5 **reaper** failures
that pass in isolation: `recover_orphans`/`mark_dead_workers` sweep the whole DB by
design, so other tests' orphaned jobs were counted by assertions on the global
`(requeued, dead_lettered)` tuple (idempotency test saw 200 orphans, not 1). Fixed
with an autouse fixture in `test_reaper.py` that `TRUNCATE`s the job/worker tables
before each reaper test — fleet isolation without a global teardown, scoped to
those tables so scaffold survives, safe because the suite only runs against
`TEST_DATABASE_URL`. **Suite now 32 passed, stable across 3 consecutive runs.**

**Documentation — four graded docs, written from live ground truth** (DB
introspection + exported OpenAPI, not memory):

- `docs/ARCHITECTURE.md` — four process types + one DB, module layering
  (router→service→model), the job lifecycle state machine, the four reliability
  guarantees and where each lives, worker internals, compose topology, data flow.
- `docs/ER-DIAGRAM.md` — 13-table Mermaid ER diagram with exact columns/types
  from the running DB, the CASCADE/RESTRICT/SET NULL policy per edge, the
  index catalogue (incl. the 21× partial-index figure), native enums.
- `docs/DESIGN-DECISIONS.md` — **32 decisions**, each choice / rejected
  alternative / why, covering everything owed across Days 0–3 plus the three
  bonuses and the deliberately-scoped-out list.
- `docs/API.md` + `docs/openapi.json` — 58-operation reference (auth, RBAC,
  error envelope, keyset pagination, idempotency, SSE) and the committed spec.

**README** updated: Documentation table now links all four docs + the spec, a new
**Bonus features** section documents how to enable AI summaries, Status shows
Day 4 **Done**.

**Corrections caught against ground truth:** `users.email` is normalized
`varchar` + plain unique index (not `citext` as the design list floated) — fixed
in the ER diagram; the decision is written up as §28.

**Verified:**

| Check | Result |
|---|---|
| Full pytest suite | **32 passed**, stable ×3 runs (was 32 with 1 known flake + 5 order-dependent) |
| AI summary disabled-by-default | `ai_summary_enabled=False`, `active_ai_provider=None` with no key; DLQ get unchanged |
| New code imports in container | config + ai_summary + dlq_service import clean |
| OpenAPI export | 42 paths / 58 operations, committed to `docs/openapi.json` |
| Stack still healthy | 8 containers up; API `/health` 200 |

---

## Remaining days

*None — Day 4 was the final day. The build is feature-complete and documented;
next action is submission before the 25 Aug deadline.*

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

Added Day 3: SSE vs WebSockets for a unidirectional feed · SSE auth via query
token (EventSource header limitation) and why it isn't a weakening · metrics as
`date_bin` aggregates gap-filled in the app vs `generate_series` in SQL ·
percentiles over succeeded-only rows · short session per SSE tick vs one held for
the stream's life · dashboard calls the API directly over CORS vs a Next.js proxy
rewrite · keyset paging surfaced as a prev/next cursor stack in the client.
