# API Reference

**58 operations across 42 paths.** This document is the human-readable map; the
machine-readable contract is committed as [`openapi.json`](openapi.json) and
served live with a Swagger UI at `http://localhost:8000/docs` (ReDoc at
`/redoc`). The two never drift — the spec is generated from the same Pydantic
models FastAPI validates against.

- **Base URL:** `http://localhost:8000`
- **API prefix:** `/api/v1` (health/readiness probes sit at the root)
- **Auth:** `Authorization: Bearer <access_token>` on everything except
  `register`, `login`, `refresh`, and the probes

---

## Conventions

### Authentication

JWT access/refresh. `register`/`login` return an access token (30 min) and a
refresh token (7 days); `refresh` mints a new access token. Tokens are decoded
with a **type check** — a refresh token cannot be used where an access token is
required, and vice versa. The user row is re-read on every request, so
deactivating an account takes effect immediately rather than at token expiry.

### Authorization (RBAC)

Roles are ranked `viewer < member < admin < owner`. Org-scoped mutations declare
a minimum role as a dependency that runs *before* the handler. Non-members
receive `404`, not `403`, so organization ids cannot be enumerated by probing.

### Error envelope

Every error — 4xx and 5xx — has one shape:

```json
{
  "error": {
    "code": "QUEUE_NOT_FOUND",
    "message": "Queue not found",
    "details": null,
    "request_id": "b3f1a2c4-…"
  }
}
```

`code` is machine-readable and stable; `message` is safe to display; `details`
carries field-level validation errors when present; `request_id` correlates a
user-facing toast with the server logs. The dashboard writes **one** error
handler, not one per endpoint.

### Pagination

List endpoints use **keyset** pagination (constant cost at any depth). The
envelope:

```json
{ "items": [ … ], "next_cursor": "opaque…", "has_more": true, "limit": 50 }
```

Pass `?cursor=<next_cursor>&limit=<n>` to page forward. The cursor is opaque and
encodes the sort tuple; `limit` is bounded by `MAX_PAGE_SIZE` (200).

### Idempotency

`POST` job-enqueue endpoints accept an `Idempotency-Key` header, enforced by a
partial-unique index — a retried enqueue returns the original job rather than a
duplicate.

---

## Endpoints by resource

### Health & readiness

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Liveness — process is up |
| GET | `/ready` | Readiness — database reachable |

### Auth

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/auth/register` | Create user + owning org (no default project) |
| POST | `/api/v1/auth/login` | Email + password → token pair |
| POST | `/api/v1/auth/refresh` | Refresh token → new access token |
| GET | `/api/v1/auth/me` | Current user |

### Organizations & members (RBAC-enforced)

| Method | Path | Min role |
|---|---|---|
| GET / POST | `/api/v1/orgs` | member / any authed |
| GET | `/api/v1/orgs/{org_id}` | member |
| PATCH | `/api/v1/orgs/{org_id}` | admin |
| DELETE | `/api/v1/orgs/{org_id}` | owner |
| GET / POST | `/api/v1/orgs/{org_id}/members` | member / admin |
| PATCH / DELETE | `/api/v1/orgs/{org_id}/members/{user_id}` | admin |
| GET / POST | `/api/v1/orgs/{org_id}/projects` | member / member |

### Projects

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/projects/{project_id}` | Access checked via org membership |
| PATCH | `/api/v1/projects/{project_id}` | |
| DELETE | `/api/v1/projects/{project_id}` | CASCADEs queues/jobs |
| POST | `/api/v1/projects/{project_id}/api-key` | Rotate project API key (returns once) |

### Retry policies

| Method | Path |
|---|---|
| GET / POST | `/api/v1/projects/{project_id}/retry-policies` |
| PATCH / DELETE | `/api/v1/retry-policies/{policy_id}` |

### Queues

| Method | Path | Notes |
|---|---|---|
| GET / POST | `/api/v1/projects/{project_id}/queues` | |
| GET / PATCH / DELETE | `/api/v1/queues/{queue_id}` | |
| GET | `/api/v1/queues/{queue_id}/stats` | Depth by status |
| POST | `/api/v1/queues/{queue_id}/pause` | Stops claiming (headroom → 0) |
| POST | `/api/v1/queues/{queue_id}/resume` | |

### Jobs

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/queues/{queue_id}/jobs` | Enqueue (immediate / delayed / scheduled); `Idempotency-Key` |
| POST | `/api/v1/queues/{queue_id}/jobs/batch` | Batch enqueue |
| GET | `/api/v1/projects/{project_id}/jobs` | Keyset-paginated explorer with filters |
| GET | `/api/v1/projects/{project_id}/jobs/{job_id}` | Job detail |
| GET | `/api/v1/projects/{project_id}/jobs/{job_id}/executions` | Immutable attempt history |
| GET | `/api/v1/projects/{project_id}/jobs/{job_id}/logs` | Structured job logs |
| POST | `/api/v1/projects/{project_id}/jobs/{job_id}/retry` | Manual retry |
| POST | `/api/v1/projects/{project_id}/jobs/{job_id}/cancel` | Cancel a pre-terminal job |

### Schedules (cron)

| Method | Path | Notes |
|---|---|---|
| GET / POST | `/api/v1/projects/{project_id}/schedules` · `/api/v1/queues/{queue_id}/schedules` | List / create |
| GET / PATCH / DELETE | `/api/v1/projects/{project_id}/schedules/{schedule_id}` | Manage; PATCH toggles `is_active` |
| POST | `/api/v1/projects/{project_id}/schedules/{schedule_id}/trigger` | Fire once now |

### Dead-letter queue

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/projects/{project_id}/dlq` | `?unreplayed_only=true` for the working set |
| GET | `/api/v1/projects/{project_id}/dlq/{entry_id}` | Detail — generates the AI summary lazily on first open |
| POST | `/api/v1/projects/{project_id}/dlq/{entry_id}/replay` | Re-enqueue as a new job (once) |
| DELETE | `/api/v1/projects/{project_id}/dlq/{entry_id}` | Discard |

### Fleet, workers & metrics

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/fleet/stats` | Fleet summary incl. `scheduler_leader_present` (read from `pg_locks`) |
| GET | `/api/v1/workers` · `/api/v1/workers/{worker_id}` | Worker fleet + heartbeat freshness |
| GET | `/api/v1/metrics/throughput` | Executions per `date_bin` bucket by outcome |
| GET | `/api/v1/metrics/latency` | `percentile_cont` p50/p95/p99 over succeeded rows |
| GET | `/api/v1/metrics/health` | Recent success/failure + queue depth |

### Live feed (SSE)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/events?token=<jwt>&interval=<s>` | `text/event-stream`; fleet + health snapshot every `interval`. Token in query because `EventSource` cannot set headers (see `DESIGN-DECISIONS.md §21`). |

---

## Regenerating the spec

`openapi.json` is exported from the running API:

```bash
curl -s http://localhost:8000/openapi.json -o docs/openapi.json
```

It is committed so the contract is reviewable in the repo without booting the
stack; the live `/docs` is the interactive version.
