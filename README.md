<div align="center">

#  Codity — Distributed Job Scheduler

**A production-inspired distributed job scheduler where PostgreSQL *is* the queue.**

Jobs are claimed atomically by a fleet of independent workers — with retry policies,
cron scheduling, crash recovery, dead-letter handling, and a live operator dashboard.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=next.js&logoColor=white)](https://nextjs.org/)
[![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![CI](https://github.com/Shardul9999/Distributed-Job-Scheduler/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Shardul9999/Distributed-Job-Scheduler/actions/workflows/ci.yml)

</div>

> **The core guarantee:** with 10 workers competing for 500 jobs, every job runs
> **exactly once** — verified under `kill -9`. No queue library, no Redis, no broker.
> Just `FOR UPDATE SKIP LOCKED` and fencing tokens.

<div align="center">

| Highlight | |
|---|---|
| 🔒 **Exactly-once execution** | `SKIP LOCKED` claiming + `lock_token` fencing |
| ♻️ **Crash recovery** | heartbeats + reaper revive a dead worker's jobs |
| ⏰ **Cron scheduling** | IANA timezones, DST-safe, no double-fire |
| 👑 **Leader election** | `pg_try_advisory_lock`, no lease, no split-brain |
| 📊 **Live dashboard** | 7 pages, SSE streaming, throughput/latency charts |
| 🧪 **48 tests** | real PostgreSQL, no mocks — run on every push by [CI](.github/workflows/ci.yml) |

</div>

---

## Deliverables

| Required | Where |
|---|---|
| Source code with setup instructions | This repository — [Quick start](#quick-start) |
| Architecture diagram | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [diagrams below](#architecture) |
| ER diagram | [docs/ER-DIAGRAM.md](docs/ER-DIAGRAM.md) · 13 tables |
| API documentation | [docs/API.md](docs/API.md) · [openapi.json](docs/openapi.json) · live at `/docs` |
| Design decisions document | [docs/DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md) · 34 trade-offs |
| Automated tests | [tests/](tests/) · 48 tests on real PostgreSQL · [how to run](#running-the-tests) |

---

## Quick start

**Prerequisites:** Docker and Docker Compose. Nothing else — no local Python,
no local PostgreSQL.

```bash
cp .env.example .env
docker compose up -d
```

That is the whole setup. The API waits for PostgreSQL to become healthy, applies
all migrations, and starts serving.

| What | Where |
|---|---|
| **Dashboard** | http://localhost:3000 |
| API | http://localhost:8000 |
| Interactive API docs (Swagger) | http://localhost:8000/docs |
| Alternative docs (ReDoc) | http://localhost:8000/redoc |
| OpenAPI schema | http://localhost:8000/openapi.json |
| Liveness probe | http://localhost:8000/health |
| Readiness probe | http://localhost:8000/ready |

Verify it is running:

```bash
curl http://localhost:8000/health
```

### Or run it without cloning

The images are published, so the whole system can be started from a single
file — no repository, no build:

```bash
curl -O https://raw.githubusercontent.com/Shardul9999/Distributed-Job-Scheduler/main/docker-compose.prod.yml
docker compose -f docker-compose.prod.yml up -d
```

| Image | Contents | Size |
|---|---|---|
| [`shardul005/codity-api`](https://hub.docker.com/r/shardul005/codity-api) | API, worker and scheduler — one image, three commands | 751 MB |
| [`shardul005/codity-web`](https://hub.docker.com/r/shardul005/codity-web) | Next.js dashboard | 995 MB |

The API, workers and scheduler share one image deliberately: they run the same
code and differ only in the command they are started with, so a single build
makes it impossible for a worker to run a different version of the shared
schema than the API does.

[`docker-compose.prod.yml`](docker-compose.prod.yml) differs from the default
compose file in two ways — it pulls the images rather than building them, and
it mounts nothing over them. The development file bind-mounts the repository
into every container for autoreload, which means the published images are never
actually exercised; this one runs exactly what was pushed.

---

## Try it

Register an account. This also creates your first organization and returns a
token pair in the same response.

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{
    "email": "you@example.com",
    "password": "choose-a-real-password",
    "full_name": "Your Name",
    "organization_name": "Acme Inc"
  }'
```

Save the `access_token` from the response, then create a project:

```bash
TOKEN="<paste access_token>"
ORG=$(curl -s http://localhost:8000/api/v1/orgs \
  -H "Authorization: Bearer $TOKEN" | python -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")

curl -X POST "http://localhost:8000/api/v1/orgs/$ORG/projects" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name": "Email Pipeline", "description": "Transactional email jobs"}'
```

---

## Dashboard

The web dashboard at **http://localhost:3000** is the operator's view of the
whole system: throughput and latency charts, queue depth, the live worker fleet,
cron schedules, and the dead-letter queue — all updating live over Server-Sent
Events.

![Overview — live fleet metrics, throughput, queue depth and latency percentiles](docs/screenshots/01-overview.png)

*Overview: three workers heartbeating with 30 slots of capacity, the elected
scheduler leader, and throughput / latency charts streaming over SSE.*

![Dead letter drawer with an AI-generated failure summary](docs/screenshots/02-dlq-ai-summary.png)

*A dead letter opened: the stack trace and original payload are kept for
forensics, and an LLM summarises the cause in one line — generated on demand
when the drawer opens. Shown here with Gemini; Groq is supported identically.*

![Team page signed in as the viewer role](docs/screenshots/03-viewer-team-rbac.png)

*Signed in as the viewer: the role badge follows you into the sidebar, the
member-management form is replaced by the reason it is unavailable, and every
rank in the organization is visible in the roster.*

To see it populated, seed a demo tenant with queues, a cron schedule, a
realistic job mix (fast successes, retries that recover, failures that
dead-letter), and a team at every role:

```bash
python scripts/seed.py
```

The script uses only the Python standard library and talks to the API over
HTTP, so it needs no dependencies. It prints two logins when it finishes:

| Role | Email | Password | |
|---|---|---|---|
| **owner** | `demo@codity.dev` | `demodemo123` | full access |
| **viewer** | `sam.okafor@codity.dev` | `teamdemo123` | read-only |

Both are at **http://localhost:3000**. Sign in as the **viewer** to see
authorization from the other side: the sidebar badge reads `viewer`, and every
write control — pause, retry, cancel, trigger, replay, discard — is disabled
with its reason on hover. A viewer who sees a greyed-out *Retry* learns the
action exists and that their role withholds it; hiding it would just look like a
bare page. The disabling is a courtesy, not the control: the same calls return
`403` made directly against the API, which is what
[`tests/test_authorization.py`](tests/test_authorization.py) asserts.

Seven pages: **Overview** (live metrics + charts), **Queues** (depth, pause /
resume), **Job Explorer** (filter, keyset paging, per-job execution history and
logs, retry / cancel), **Workers** (fleet with heartbeat freshness),
**Schedules** (cron entries, next fire time, manual trigger), **Dead Letters**
(failure inspection and replay), and **Team** (roster, invite by email, change
role, remove).

**Design language.** The dashboard mirrors Codity's own console: its surface,
border, text and status tokens verbatim (`#090909` app, `#0d0d0d` surface,
`#3ec98a` success, `#ff5b52` danger), in **Archivo** with **JetBrains Mono** for
machine-written values. The accent is *lifted* to `#7a7fe0` because codity.ai's
marketing indigo is unreadable on near-black — same hue, so the brand survives.
One theme, deliberately: a monitoring surface that sits open on a second screen
does not need two palettes, each doubling the work of keeping charts, badges and
hover washes legible.

Live updates use SSE rather than WebSockets — the feed is strictly
server→client, so `EventSource` (which reconnects natively) is the right tool.
Because `EventSource` cannot set an `Authorization` header, the `/events`
endpoint accepts the access token as a query parameter, validated exactly as the
header form is.

---

## Architecture

Four independently deployable process types share one PostgreSQL database:

```mermaid
flowchart TB
    subgraph browser["🌐 Browser"]
        WEB["<b>Next.js Dashboard</b><br/>7 pages · Recharts · live SSE"]
    end

    subgraph apitier["🔌 API tier — stateless, scales horizontally"]
        API["<b>FastAPI</b> × N<br/>58 endpoints · JWT + RBAC<br/>validation · SSE feed"]
    end

    subgraph datatier["🐘 Data tier — the queue IS the database"]
        PG[("<b>PostgreSQL 16</b><br/>13 tables · partial indexes<br/>SKIP LOCKED · advisory locks")]
    end

    subgraph compute["⚙️ Compute tier — independently scalable"]
        WORKER["<b>worker</b> × N<br/>claim → execute → ack<br/>heartbeat · graceful drain"]
        SCHED["<b>scheduler</b> × 2<br/>1 leader · 1 standby<br/>cron + reaper"]
    end

    WEB -->|"REST + SSE"| API
    API -->|"enqueue · read · aggregate"| PG
    WORKER <-->|"FOR UPDATE SKIP LOCKED<br/>fenced by lock_token"| PG
    SCHED <-->|"pg_try_advisory_lock<br/>materialise cron · revive orphans"| PG

    classDef front fill:#0ea5e9,stroke:#0369a1,color:#fff
    classDef back fill:#8b5cf6,stroke:#6d28d9,color:#fff
    classDef db fill:#4169E1,stroke:#1e3a8a,color:#fff
    classDef proc fill:#10b981,stroke:#047857,color:#fff
    class WEB front
    class API back
    class PG db
    class WORKER,SCHED proc
```

**The queue is PostgreSQL itself**, not Redis or a message broker. Jobs are
claimed with a single atomic statement:

```sql
SELECT id FROM jobs
WHERE status IN ('queued','scheduled') AND run_at <= now()
ORDER BY priority DESC, run_at ASC
FOR UPDATE SKIP LOCKED
LIMIT :n
```

`SKIP LOCKED` makes competing workers step over rows another transaction already
holds, so N workers claim N disjoint batches with no contention and no blocking.
Full reasoning in [docs/DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md).

### How exactly-once survives a concurrent fleet

```mermaid
sequenceDiagram
    autonumber
    participant A as Worker A
    participant B as Worker B
    participant PG as PostgreSQL

    par Concurrent claims
        A->>PG: CLAIM · FOR UPDATE SKIP LOCKED
    and
        B->>PG: CLAIM · FOR UPDATE SKIP LOCKED
    end
    PG-->>A: jobs 1-10 · lock_token = α
    PG-->>B: jobs 11-20 · lock_token = β
    Note over A,B: Disjoint sets — B steps over A's locked rows

    A->>A: execute handler
    A--xA: 💥 process dies mid-job
    Note over PG: heartbeat goes silent

    PG->>PG: reaper: worker dead → job LOST<br/>requeue · lock_token = NULL
    PG-->>B: job 1 reclaimed · lock_token = γ
    B->>PG: complete WHERE lock_token = γ ✅

    Note over A: zombie A revives, reports success
    A->>PG: complete WHERE lock_token = α
    PG-->>A: 0 rows matched — discarded 🛡️
```

The last two steps are the point: a revived zombie holds a **stale token**, so its
write matches zero rows. Recovery without fencing would just be a slower path to
running a job twice.

### Job lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: enqueue
    [*] --> scheduled: delayed / cron

    scheduled --> queued: run_at reached
    queued --> claimed: atomic claim
    claimed --> running: handler starts

    running --> completed: ✅ success
    running --> queued: 🔁 retry with backoff
    running --> dead: ☠️ attempts exhausted → DLQ

    claimed --> queued: ♻️ reaper (worker died)
    running --> queued: ♻️ reaper (worker died)

    queued --> cancelled: operator cancels
    dead --> queued: DLQ replay (new job)

    completed --> [*]
    cancelled --> [*]
```

Two failure paths, deliberately asymmetric: a **handler that raised** consumes a
retry and backs off; a **worker that vanished** also consumes the attempt, because
a lost job may have had side effects. A job *released* during graceful shutdown is
refunded — it never ran.

---

## Bonus features

The assignment listed eight; **three are implemented** and the other five are
deliberately scoped out with a recorded rationale — full table in
[docs/DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md) §30–32 (RBAC in §31 and
§31a).

| Bonus | Status |
|---|---|
| Distributed locking | **Built** |
| Role-based access control | **Built** |
| AI-generated failure summaries | **Built** |
| WebSocket live updates | Live updates **built via SSE** — chosen over WebSockets for a server→client-only feed |
| Event-driven execution | Polling with backoff; `LISTEN/NOTIFY` documented as the upgrade path |
| Workflow dependencies | Scoped out — `jobs.depends_on` reserved |
| Rate limiting | Scoped out — `queues.rate_limit_per_sec` reserved |
| Queue sharding | Scoped out — partial claim index keeps latency flat without it |

The three that are built:

- **Distributed locking** — the scheduler is a leader-elected singleton via
  `pg_try_advisory_lock` on a dedicated session-scoped connection. No lease, no
  TTL, no split-brain; a `kill -9` on the leader frees the lock automatically.
- **RBAC** — ranked roles (`viewer < member < admin < owner`) enforced by
  dependencies that run before the handler, at every scope a resource is
  addressed by: organization, project, queue and retry policy. Each resolver
  proves tenancy and reads the caller's role in one query, so the rank check
  costs no extra round trip. Two refusals, for two reasons: outside the
  organization is `404` so ids can't be enumerated, under-ranked inside it is
  `403`. Handing out roles is separately constrained — nobody may grant a role
  above their own or modify a member who outranks them, without which an admin
  could promote themselves to owner and remove the founder. Visible in the
  product on the **Team** page, and covered by 12 tests in
  [`tests/test_authorization.py`](tests/test_authorization.py).
- **AI failure summaries** — a dead-lettered job's stack trace is summarised into
  one plain-English cause on the DLQ page. **Optional and provider-agnostic:**
  set `GROQ_API_KEY` *or* `GEMINI_API_KEY` in `.env` to enable it (free tiers:
  [Groq](https://console.groq.com/keys), [Gemini](https://aistudio.google.com/apikey)).
  With no key set, the feature is inert — `ai_summary` stays null and the system
  behaves identically. Generated lazily on first inspection, then cached; any
  provider error degrades silently to no summary.

---

## Project layout

```
apps/
  api/            FastAPI service
    core/         config, security, deps, errors, logging, pagination
    routers/      one module per resource group
    schemas/      Pydantic request/response contracts
    services/     business logic (routers stay thin)
  worker/         claim loop, executor, handlers, heartbeat
  scheduler/      leader election, cron materialization, reaper
  web/            Next.js dashboard (frontend)
packages/
  db/             SQLAlchemy models, enums, session, Alembic migrations
tests/            pytest suite, incl. the 10-worker concurrency test
docs/             plan, architecture, ER diagram, design decisions
scripts/          container entrypoint, demo seed script
```

---

## Database

Thirteen tables:

| Concern | Tables |
|---|---|
| Identity & tenancy | `users`, `organizations`, `organization_members`, `projects` |
| Queue configuration | `retry_policies`, `queues` |
| Job data | `jobs`, `job_executions`, `job_logs`, `scheduled_jobs`, `dead_letter_queue` |
| Worker fleet | `workers`, `worker_heartbeats` |

Full schema rationale — keys, indexes, normalization, cascade policy — is in
[docs/PLAN.md](docs/PLAN.md) §4 and inline in
[packages/db/models.py](packages/db/models.py).

### Migrations

Applied automatically on API startup. To run them manually:

```bash
docker compose run --rm api alembic upgrade head
```

After changing a model, generate a migration:

```bash
docker compose run --rm api alembic revision --autogenerate -m "describe change"
```

Always read the generated file before committing it — autogenerate does not
detect every change, particularly enum value additions.

---

## Development

```bash
docker compose logs -f api        # follow API logs
docker compose restart api        # restart after a dependency change
docker compose down               # stop everything
docker compose down -v            # stop and DESTROY the database volume
```

Application code is bind-mounted, so edits reload automatically without a
rebuild. Rebuild only when `requirements.txt` changes:

```bash
docker compose build api
```

Open a database shell:

```bash
docker exec -it codity-postgres psql -U codity -d codity
```

### Running the tests

The suite needs a real PostgreSQL and builds the schema with the real
migrations, so the simplest way is to run it inside the API container against a
database of its own:

```bash
docker compose exec postgres psql -U codity -d postgres -c "CREATE DATABASE codity_test OWNER codity;"

docker compose exec \
  -e TEST_DATABASE_URL="postgresql+asyncpg://codity:codity_dev_password@postgres:5432/codity_test" \
  api python -m pytest -q          # 48 passed
```

Use a **separate database**, not the running `codity` one. Workers poll every
unpaused queue, so a live fleet can claim jobs a test just inserted and the
concurrency tests may then see a short count. `TEST_DATABASE_URL` is optional —
without it the suite starts its own PostgreSQL via Testcontainers, which is what
lets the same tests run on a laptop and inside the container where Docker is not
reachable.

The same suite runs on every push via
[GitHub Actions](.github/workflows/ci.yml), alongside a frontend typecheck and a
`docker compose build` — because the quick-start promise is `docker compose up`,
and a broken image is a broken submission even with every test green.

---

## Conventions

**Error envelope.** Every error response, without exception, has this shape:

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Project not found",
    "details": null,
    "request_id": "5d27798409d74581a6d78df78b10a39b"
  }
}
```

`request_id` is also returned in the `X-Request-ID` header and stamped on every
server log line for that request, so any user-visible failure can be traced to
its logs.

**Pagination** is keyset, not offset: `?cursor=<opaque>&limit=50`. Cost stays
constant regardless of depth, and rows are never skipped or duplicated when the
underlying table is being written to concurrently.

**Authentication** is JWT bearer, with separate access (30 min) and refresh
(7 day) tokens distinguished by a `type` claim that is verified on decode.

---

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Four process types, module layering, job lifecycle, reliability guarantees |
| [docs/ER-DIAGRAM.md](docs/ER-DIAGRAM.md) | 13-table ER diagram, FK cascade policy, indexes, enums |
| [docs/API.md](docs/API.md) | Endpoint reference — 58 operations |
| [docs/openapi.json](docs/openapi.json) | Machine-readable OpenAPI spec (also live at `/docs`) |
| [docs/DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md) | 34 decisions with rejected alternatives and rationale |
| [docs/PLAN.md](docs/PLAN.md) | Full implementation plan, schema, build schedule |

---

## Status

Feature-complete. The four-day plan landed in full: schema, migrations and auth;
queue and job APIs over the atomic claim; retries, dead letters, cron and the
reaper; the dashboard with live SSE; and the documentation set, with three
bonuses built (distributed locking · RBAC · AI failure summaries).

Hardened after the build: a fleet-wide concurrency cap, role enforcement scoped
to project, queue and retry policy, role-administration rules that close a
privilege-escalation path, and recovery for a live worker wrongly declared dead.
