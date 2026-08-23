#!/usr/bin/env python3
"""Seed a demo tenant with queues, a schedule, and a realistic job mix.

Run after `docker compose up` to give the dashboard something to show: a known
login, a few queues, a per-minute cron schedule, and a spread of jobs that
exercise every path the UI surfaces -- fast successes, variable-latency work,
retries that recover, and failures that dead-letter.

It talks to the API over HTTP rather than the database directly, on purpose:
seeding through the public contract proves the same endpoints the dashboard
uses, and keeps this script free of the ORM. Standard library only, so it runs
anywhere Python does without installing anything.

    python scripts/seed.py                       # against localhost:8000
    API_BASE=http://localhost:8000 python scripts/seed.py

Re-runnable: it logs in if the demo user already exists and reuses a project,
queue or schedule of the same name rather than duplicating it.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.getenv("API_BASE", "http://localhost:8000").rstrip("/")
PREFIX = f"{API_BASE}/api/v1"

DEMO_EMAIL = os.getenv("SEED_EMAIL", "demo@codity.dev")
DEMO_PASSWORD = os.getenv("SEED_PASSWORD", "demodemo123")
DEMO_NAME = "Demo Operator"
DEMO_ORG = "Acme Corp"
PROJECT_NAME = "Production"

# A roster at every rank, so the Team page is not an empty table and the role
# system can be seen working rather than taken on trust. The viewer login is
# the useful one: signed in as Sam, every write control on the dashboard is
# disabled with a reason, and the API refuses the same actions with 403.
TEAM_PASSWORD = os.getenv("SEED_TEAM_PASSWORD", "teamdemo123")
VIEWER_EMAIL = "sam.okafor@codity.dev"
TEAM = [
    ("priya.desai@codity.dev", "Priya Desai", "admin"),
    ("marco.silva@codity.dev", "Marco Silva", "member"),
    ("ana.kowalski@codity.dev", "Ana Kowalski", "member"),
    (VIEWER_EMAIL, "Sam Okafor", "viewer"),
]

_token: str | None = None


def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    url = f"{PREFIX}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if _token:
        req.add_header("Authorization", f"Bearer {_token}")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def die(msg: str, detail: object = None) -> None:
    print(f"[!!] {msg}", file=sys.stderr)
    if detail is not None:
        print(f"  {detail}", file=sys.stderr)
    sys.exit(1)


def authenticate() -> None:
    """Register the demo user, or log in if they already exist."""
    global _token
    status, body = call(
        "POST",
        "/auth/register",
        {
            "email": DEMO_EMAIL,
            "password": DEMO_PASSWORD,
            "full_name": DEMO_NAME,
            "organization_name": DEMO_ORG,
        },
    )
    if status in (200, 201):
        _token = body["access_token"]  # type: ignore[index]
        print(f"[ok] Registered {DEMO_EMAIL}")
        return

    # Already exists (409) or similar -> log in.
    status, body = call(
        "POST", "/auth/login", {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    )
    if status != 200:
        die("Could not register or log in the demo user", body)
    _token = body["access_token"]  # type: ignore[index]
    print(f"[ok] Logged in as {DEMO_EMAIL}")


def get_org_id() -> str:
    status, orgs = call("GET", "/orgs")
    if status != 200 or not orgs:
        die("No organizations for the demo user", orgs)
    return orgs[0]["id"]  # type: ignore[index]


def ensure_project(org_id: str) -> str:
    status, projects = call("GET", f"/orgs/{org_id}/projects")
    if status == 200:
        for p in projects:  # type: ignore[union-attr]
            if p["name"] == PROJECT_NAME:
                print(f"[--] Project '{PROJECT_NAME}' already exists")
                return p["id"]
    status, proj = call(
        "POST",
        f"/orgs/{org_id}/projects",
        {"name": PROJECT_NAME, "description": "Seeded demo workload"},
    )
    if status not in (200, 201):
        die("Failed to create project", proj)
    print(f"[ok] Created project '{PROJECT_NAME}'")
    return proj["id"]  # type: ignore[index]


def ensure_queue(project_id: str, name: str, **cfg) -> str:
    status, queues = call("GET", f"/projects/{project_id}/queues")
    if status == 200:
        for q in queues:  # type: ignore[union-attr]
            if q["name"] == name:
                return q["id"]
    status, q = call(
        "POST", f"/projects/{project_id}/queues", {"name": name, **cfg}
    )
    if status not in (200, 201):
        die(f"Failed to create queue '{name}'", q)
    print(f"[ok] Created queue '{name}'")
    return q["id"]  # type: ignore[index]


def ensure_schedule(project_id: str, queue_id: str, name: str, **cfg) -> None:
    status, schedules = call("GET", f"/projects/{project_id}/schedules")
    if status == 200:
        for s in schedules:  # type: ignore[union-attr]
            if s["name"] == name:
                print(f"[--] Schedule '{name}' already exists")
                return
    status, s = call("POST", f"/queues/{queue_id}/schedules", {"name": name, **cfg})
    if status not in (200, 201):
        die(f"Failed to create schedule '{name}'", s)
    print(f"[ok] Created schedule '{name}'")


def ensure_team(org_id: str) -> None:
    """Give the demo organization a member at every role.

    Each teammate is registered first, because `POST /orgs/{id}/members` adds an
    *existing* account by email -- the same constraint a real admin hits. Their
    own organization (created by registration) is harmless and unused; they work
    inside Acme Corp through the membership added here.
    """
    status, existing = call("GET", f"/orgs/{org_id}/members")
    known = {m["email"] for m in existing} if status == 200 else set()

    added = 0
    for email, name, role in TEAM:
        if email in known:
            continue
        # 409 means the account exists from an earlier run -- fine, we only need
        # it to exist before the membership is created.
        call(
            "POST",
            "/auth/register",
            {
                "email": email,
                "password": TEAM_PASSWORD,
                "full_name": name,
                "organization_name": f"{name} Personal",
            },
        )
        status, body = call(
            "POST", f"/orgs/{org_id}/members", {"email": email, "role": role}
        )
        if status in (200, 201):
            added += 1
        elif status != 409:
            die(f"Failed to add {email} as {role}", body)

    if added:
        print(f"[ok] Added {added} teammates (admin, member x2, viewer)")
    else:
        print("[--] Team already seeded")


def enqueue_batch(queue_id: str, jobs: list[dict]) -> int:
    status, resp = call("POST", f"/queues/{queue_id}/jobs/batch", {"jobs": jobs})
    if status not in (200, 201):
        die("Batch enqueue failed", resp)
    return resp["created"]  # type: ignore[index]


def main() -> None:
    print(f"Seeding {PREFIX} ...\n")
    authenticate()
    org_id = get_org_id()
    ensure_team(org_id)
    project_id = ensure_project(org_id)

    default_q = ensure_queue(
        project_id, "default", priority=0, max_concurrency=10, default_timeout_s=30
    )
    emails_q = ensure_queue(
        project_id, "emails", priority=10, max_concurrency=5, default_timeout_s=30
    )
    reports_q = ensure_queue(
        project_id, "reports", priority=-10, max_concurrency=2, default_timeout_s=120
    )

    ensure_schedule(
        project_id,
        default_q,
        "minute-heartbeat",
        cron_expression="* * * * *",
        timezone="UTC",
        job_type="echo",
        payload={"source": "cron", "note": "per-minute heartbeat"},
    )

    total = 0
    # Fast successes -- the bulk of a healthy throughput chart.
    total += enqueue_batch(
        default_q,
        [{"job_type": "echo", "payload": {"n": i}} for i in range(40)],
    )
    # Variable-latency work -- gives the latency percentiles a spread.
    total += enqueue_batch(
        reports_q,
        [
            {"job_type": "sleep", "payload": {"seconds": round(0.3 + (i % 6) * 0.4, 1)}}
            for i in range(18)
        ],
    )
    # Emails: a few that fail once or twice then recover, exercising retry+backoff.
    total += enqueue_batch(
        emails_q,
        [
            {
                "job_type": "fail",
                "payload": {
                    "fail_until_attempt": 2,
                    "message": "SMTP timeout",
                },
                "max_attempts": 4,
            }
            for _ in range(8)
        ],
    )
    # A handful that always fail -> exhaust retries -> dead-letter queue.
    total += enqueue_batch(
        emails_q,
        [
            {
                "job_type": "fail",
                "payload": {"message": "Recipient address rejected"},
                "max_attempts": 2,
            }
            for _ in range(5)
        ],
    )

    print(f"\n[ok] Enqueued {total} jobs across 3 queues")
    print("\n" + "=" * 60)
    print("  Dashboard:  http://localhost:3000")
    print(f"  Owner:      {DEMO_EMAIL} / {DEMO_PASSWORD}")
    print(f"  Viewer:     {VIEWER_EMAIL} / {TEAM_PASSWORD}")
    print("=" * 60)
    print("\nWorkers will drain these within seconds; the failing")
    print("emails will land in Dead Letters after their retries.")
    print("\nSign in as the viewer to see RBAC from the other side: every")
    print("write control is disabled with a reason, and the API refuses the")
    print("same actions with 403 even if you call them directly.")


if __name__ == "__main__":
    main()
