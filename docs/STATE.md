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

**The assignment's full bonus list (all 8), recorded verbatim so it is never
lost again** — outcome for each is in DESIGN-DECISIONS.md §30–32:

| Bonus | Outcome |
|---|---|
| Workflow dependencies | scoped out (`jobs.depends_on` reserved) |
| Rate limiting | scoped out (`queues.rate_limit_per_sec` reserved) |
| **Distributed locking** | **BUILT** |
| Queue sharding | scoped out |
| Event-driven execution | scoped out; polling + backoff, `LISTEN/NOTIFY` = upgrade path |
| WebSocket live updates | live updates **built via SSE**, WebSockets rejected with rationale |
| **Role-based access control** | **BUILT** |
| **AI-generated failure summaries** | **BUILT** |
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

**Test suite** (`tests/`, pytest + testcontainers): **45 tests, all passing.**
Real PostgreSQL, schema built by `alembic upgrade head` (not `create_all` --
the migration is what ships). `TEST_DATABASE_URL` points the suite at an
existing database, which is how it runs inside the API container.

```bash
docker compose exec -T -e TEST_DATABASE_URL="postgresql+asyncpg://codity:codity_dev_password@postgres:5432/codity_test"   api python -m pytest tests/ -q          # 45 passed
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
`TEST_DATABASE_URL`. **Suite now 41 passed, stable across 3 consecutive runs.**

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
| Full pytest suite | **41 passed**, stable ×3 runs (was 32 with 1 known flake + 5 order-dependent) |
| AI summary disabled-by-default | `ai_summary_enabled=False`, `active_ai_provider=None` with no key; DLQ get unchanged |
| New code imports in container | config + ai_summary + dlq_service import clean |
| OpenAPI export | 42 paths / 58 operations, committed to `docs/openapi.json` |
| Stack still healthy | 8 containers up; API `/health` 200 |

---

## Post-Day-4 fix — fleet-wide concurrency cap (committed, pushed)

**Found a real bug in the claim path and fixed it.** `max_concurrency` is a
*fleet-wide* cap, but concurrent claimers each read the in-flight count under
`READ COMMITTED`, all computed the same headroom, and each took a full
allowance: **10 workers vs a cap of 3 put 30 jobs in flight**. The overshoot
factor equals the fleet size. Not a duplicate-execution bug — `SKIP LOCKED` still
gave every worker distinct rows — a *capacity* bug, which matters because the
setting exists to shield downstream dependencies.

**Why the suite missed it:** the `engine` fixture is function-scoped, so every
test run started with a **cold connection pool**; establishing 10 asyncpg
connections staggered the claimers so they never overlapped. Cold round → correct
3. Every warm round → 30. The suite only ever ran the cold one.

**The fix** (`packages/db/claim.py`): `LOCK_QUEUE_SQL`, a row lock on the queue,
taken as its **own statement** before `CLAIM_SQL` in the same transaction. The
separate-statement part is essential — a `FOR UPDATE` CTE inside the claim
serialises claimers but does *not* give a waiter a fresh snapshot on wake
(`READ COMMITTED` snapshots are per-statement), so it still measured 21 in flight
against a cap of 3. A preceding statement means the claim runs with a new
snapshot that sees the previous claimer's committed work.

| Check | Before | After |
|---|---|---|
| 40 rounds, 10 workers, cap 3 | **39/40 exceeded** (up to 30) | **0/40**, exactly 3 every round |
| Claim throughput, one hot queue | 8,630/s | 4,135/s (~2×, acceptable) |
| Live: capped queue, 3 worker containers | — | pinned at 2, all 8 drained, 0 dup `(job, attempt)` |
| Test suite | 32 passed | **33 passed**, stable ×3 |

Regression test `test_concurrency_cap_holds_with_a_warm_pool` warms the pool
first and asserts DB row counts, not what the claim reports about itself.
**Verified it fails on the old query** (18–30 in flight) before trusting it.
Written up as DESIGN-DECISIONS.md §6a.

---

## Post-Day-4 fix — authorization below the organization (committed, pushed)

**Trigger.** Reviewing whether the "implement authentication" requirement was
satisfied. Core auth was fine — only five endpoints are public (`register`,
`login`, `refresh`, `health`, `ready`); everything else needs a bearer token.
The gap was RBAC: `require_role` reads `org_id` from the path, and **21 write
endpoints are addressed by `project_id` / `queue_id` / `policy_id` instead**, so
they only ever checked *membership*. A `viewer` could delete a project.

**A real vulnerability, not just a missing check.** `PATCH` and `DELETE
/retry-policies/{policy_id}` loaded the row with a bare `db.get()` — no join to
an organization at all. Reproduced against the running stack: a second,
unrelated org's token changed another tenant's `max_attempts` from 3 to 99, then
deleted the policy. Both returned 200/204. These were the only writes addressed
by an id with no `project_id` in the path — exactly the shape the single-scope
dependency could not cover.

**Fix.** Three resolvers in `apps/api/core/deps.py` — `require_project_role`,
`require_queue_role`, `require_policy_role` — each walking resource → project →
org → membership in **one** query that returns the resource *and* the caller's
role. The tenancy join had to run anyway, so ranking the role costs nothing
extra. Exposed as named aliases (`WritableQueue`, `AdminProject`, …) so a
handler's signature states the privilege it demands. `_authorized_queue` in
`routers/queues.py` became dead code and was removed.

Line: `viewer` reads everything and writes nothing · `member` operates (enqueue,
retry, cancel, pause, replay) · `admin` destroys history or issues credentials
(delete project/queue, rotate API key) · `owner` everything. Outsiders get
`404`, under-ranked members get `403` — different refusals for different
reasons.

| Check | Before | After |
|---|---|---|
| Ungated write routes | 21 | 0 |
| Cross-tenant policy edit/delete | **200 / 204** | 404, row unchanged |
| Auth tests in the suite | **0** | 8 (`tests/test_authorization.py`) |
| Full suite | 33 passed | **41 passed** |

`tests/test_authorization.py` drives the real ASGI app over `httpx` with only
`get_session` overridden — authorization lives in the dependency chain, so a
service-level test would bypass the thing under test entirely.

**Why `TEST_DATABASE_URL` must point at `codity_test`, not `codity`.** Workers
poll *every* unpaused queue (`_target_queues`, and no `WORKER_QUEUES` is set), so
the live fleet can claim jobs a test just inserted. The test's own claimers give
up after two consecutive empty rounds, so a stolen chunk shows up as a short
count rather than an error — observed once as `test_exactly_once_execution_end_to_end`
failing `170 == 200`, with the no-double-execution assertion still passing.

Frequency: **once**, and not reproducible on demand — 19 subsequent runs against
live `codity` all passed, including with the fleet held in fast-poll mode by a
2,140-job backlog. Treat it as a real but rare interference, not a certainty. An
isolated database removes the variable for free, which is why the command above
uses one.

DESIGN-DECISIONS.md §31 rewritten: it previously claimed "a route physically
cannot forget its check", which was only true of routes that declared one.

---

## Post-Day-4 — RBAC made visible, and two holes it exposed (committed, pushed)

**Why.** The dashboard had exactly one auth screen (login), so the RBAC bonus was
invisible in the product: a grader clicking around saw a single-user tool, and
the only way to see or change a role was curl. Building the missing screens then
surfaced two real defects behind them.

**Screens.** `/register` (self-service sign-up; the endpoint creates user + org +
token pair in one call, so there is no second "create your org" step), `/team`
(roster, invite by email, change role, remove, plus a what-each-role-may-do
legend), a role badge in the sidebar, and a `Team` nav entry. `useAuth` now
exposes `role` and `can(minimum)`, resolved from the *selected project's* org
rather than read once at login — the project switcher spans every org the user
belongs to, so the role has to follow the selection.

Write controls on Queues, Jobs, Schedules and DLQ are **disabled with a reason**
rather than hidden: a viewer who sees a greyed-out "Retry" learns the action
exists and that their role withholds it; a viewer who sees nothing just thinks
the page is bare. The client-side rank table mirrors the server's and is
explicitly a courtesy — every gated action is refused again server-side, and
`test_viewer_writes_nothing` is what proves it.

**Hole 1 — cross-tenant retry-policy writes.** Covered in the previous entry.

**Hole 2 — privilege escalation through role administration.** `PATCH
/orgs/{id}/members/{user}` requires `admin` and accepted *any* target role.
Reproduced end to end: an admin set their own row to `owner` (now two owners, so
the last-owner guard stopped applying), demoted the founder to `viewer`, then
removed them. The founder ended as a non-member of the organization they
created. Fixed with two rules in `org_service` — `_assert_may_grant` (never hand
out a role above your own) and `_assert_may_target` (never modify or remove
someone who outranks you). Equal rank stays allowed, or an org would need its
owner for routine admin work. The member routes now bind `require_role(...)`'s
return value instead of discarding it, because both rules need the actor's rank.
`ROLE_RANK` moved to `packages/db/enums.py` beside `OrgRole`: authorization and
administration must never disagree about which role outranks which.

| Check | Before | After |
|---|---|---|
| Admin promotes self to owner | **200** | 403 |
| Admin demotes / removes the founding owner | **200 / 204** | 403 |
| Admin invites a new owner directly | **200** | 403 |
| Ordinary admin work (grant ≤ admin, remove a member) | 200 | 200 (unchanged) |
| Last-owner protection | held | held |
| Full suite | 41 passed | **45 passed** |

Verified live as well as in pytest: a 14-case role-administration matrix and a
22-case walk through the exact calls the new screens make, both green.

**Still true and deliberate:** registration is open and unrate-limited (any
visitor can create an account and their own org — blast radius is rows, not
data, since cross-tenant isolation is tested); and `POST /orgs/{id}/members`
returns 404 for an address with no account, which lets an *admin* probe whether
an email is registered. Both are acceptable at this scope and named here so they
read as decisions rather than oversights.

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
