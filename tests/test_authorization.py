"""Authorization: cross-tenant isolation and role enforcement.

These run through the real ASGI app rather than against the services directly.
Authorization lives in FastAPI dependencies -- `require_project_role` and
friends resolve the path id, prove tenancy, and rank the caller's role *before*
the handler body runs. Calling a service function in a test would bypass the
dependency chain entirely and assert nothing about the thing being protected.

Two properties are under test, and they are different:

* **Tenancy** -- a caller outside the owning organization gets `404`, never
  `403`. A 403 would confirm the id exists and let an outsider enumerate other
  tenants' resources by probing.
* **Role** -- a caller inside the organization but below the required rank gets
  `403`, because at that point the resource's existence is not a secret.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.api.main import app
from packages.db import get_session

PASSWORD = "Test-Passw0rd!"
API = "/api/v1"


@pytest_asyncio.fixture
async def client(engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """The real app, pointed at the test database.

    Only `get_session` is overridden. Every dependency that matters here --
    bearer decoding, the user lookup, the role resolvers -- runs exactly as it
    does in production.
    """
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _register(client: AsyncClient, tag: str) -> tuple[str, str]:
    """Create an account and its first organization. Returns (token, email)."""
    suffix = uuid.uuid4().hex[:8]
    email = f"{tag}-{suffix}@example.com"
    r = await client.post(
        f"{API}/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": tag,
            "organization_name": f"{tag} Co {suffix}",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"], email


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def tenant(client: AsyncClient) -> dict:
    """An organization with one project, queue and retry policy, plus a member
    at every role: owner, admin, member, viewer."""
    owner, _ = await _register(client, "owner")
    h = _auth(owner)

    org = (await client.get(f"{API}/orgs", headers=h)).json()[0]["id"]
    project = (
        await client.post(
            f"{API}/orgs/{org}/projects", headers=h, json={"name": "P"}
        )
    ).json()["id"]
    policy = (
        await client.post(
            f"{API}/projects/{project}/retry-policies",
            headers=h,
            json={
                "name": "pol",
                "strategy": "exponential",
                "max_attempts": 3,
                "base_delay_ms": 1000,
                "max_delay_ms": 60000,
            },
        )
    ).json()["id"]
    queue = (
        await client.post(
            f"{API}/projects/{project}/queues",
            headers=h,
            json={"name": "q", "max_concurrency": 5},
        )
    ).json()["id"]

    tokens = {"owner": owner}
    for role in ("admin", "member", "viewer"):
        token, email = await _register(client, role)
        r = await client.post(
            f"{API}/orgs/{org}/members",
            headers=h,
            json={"email": email, "role": role},
        )
        assert r.status_code == 201, r.text
        tokens[role] = token

    return {
        "org": org,
        "project": project,
        "policy": policy,
        "queue": queue,
        "tokens": tokens,
    }


async def _send(client: AsyncClient, method: str, url: str, headers: dict, body=None):
    if body is None:
        return await getattr(client, method)(url, headers=headers)
    return await getattr(client, method)(url, headers=headers, json=body)


# =============================================================================
# Tenancy
# =============================================================================


@pytest.mark.asyncio
async def test_outsider_gets_404_not_403(client: AsyncClient, tenant: dict) -> None:
    """A valid token from an unrelated organization must reveal nothing.

    `/retry-policies/{id}` is the regression that motivated this test: it took a
    bare id and never joined through to a membership, so any authenticated user
    could edit or delete any organization's retry policy by id alone.
    """
    outsider, _ = await _register(client, "outsider")
    h = _auth(outsider)
    p = tenant["project"]
    q = tenant["queue"]
    pol = tenant["policy"]

    cases = [
        ("patch", f"{API}/retry-policies/{pol}", {"max_attempts": 99}),
        ("delete", f"{API}/retry-policies/{pol}", None),
        ("get", f"{API}/queues/{q}", None),
        ("patch", f"{API}/queues/{q}", {"max_concurrency": 1}),
        ("delete", f"{API}/queues/{q}", None),
        ("post", f"{API}/queues/{q}/jobs", {"job_type": "x", "payload": {}}),
        ("post", f"{API}/queues/{q}/pause", None),
        ("get", f"{API}/projects/{p}/queues", None),
        ("delete", f"{API}/projects/{p}", None),
        ("post", f"{API}/projects/{p}/api-key", None),
    ]
    for method, url, body in cases:
        r = await _send(client, method, url, h, body)
        assert r.status_code == 404, f"{method.upper()} {url} -> {r.status_code}"


@pytest.mark.asyncio
async def test_outsider_cannot_mutate_another_tenant(
    client: AsyncClient, tenant: dict
) -> None:
    """The 404 above is a real refusal, not a cosmetic status code."""
    outsider, _ = await _register(client, "outsider")
    pol = tenant["policy"]
    owner_h = _auth(tenant["tokens"]["owner"])
    listing = f"{API}/projects/{tenant['project']}/retry-policies"

    before = (await client.get(listing, headers=owner_h)).json()
    await client.patch(
        f"{API}/retry-policies/{pol}",
        headers=_auth(outsider),
        json={"max_attempts": 99},
    )
    await client.delete(f"{API}/retry-policies/{pol}", headers=_auth(outsider))
    after = (await client.get(listing, headers=owner_h)).json()

    assert after == before, "an outsider changed another organization's policy"


# =============================================================================
# Roles
# =============================================================================


@pytest.mark.asyncio
async def test_viewer_reads_everything(client: AsyncClient, tenant: dict) -> None:
    """A VIEWER is a real member: every read in the dashboard must work."""
    h = _auth(tenant["tokens"]["viewer"])
    p = tenant["project"]
    q = tenant["queue"]

    for url in [
        f"{API}/projects/{p}/queues",
        f"{API}/projects/{p}/retry-policies",
        f"{API}/queues/{q}",
        f"{API}/queues/{q}/stats",
        f"{API}/projects/{p}/jobs",
        f"{API}/projects/{p}/dlq",
    ]:
        r = await client.get(url, headers=h)
        assert r.status_code == 200, f"GET {url} -> {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_viewer_writes_nothing(client: AsyncClient, tenant: dict) -> None:
    """Read-only means read-only, including the destructive operations that
    were reachable by any member before roles were enforced below the org."""
    h = _auth(tenant["tokens"]["viewer"])
    p = tenant["project"]
    q = tenant["queue"]
    pol = tenant["policy"]

    cases = [
        ("post", f"{API}/queues/{q}/jobs", {"job_type": "x", "payload": {}}),
        ("post", f"{API}/queues/{q}/pause", None),
        ("post", f"{API}/queues/{q}/resume", None),
        ("patch", f"{API}/queues/{q}", {"max_concurrency": 1}),
        ("delete", f"{API}/queues/{q}", None),
        ("post", f"{API}/projects/{p}/queues", {"name": "z"}),
        ("patch", f"{API}/retry-policies/{pol}", {"max_attempts": 9}),
        ("delete", f"{API}/retry-policies/{pol}", None),
        ("patch", f"{API}/projects/{p}", {"name": "renamed"}),
        ("delete", f"{API}/projects/{p}", None),
        ("post", f"{API}/projects/{p}/api-key", None),
    ]
    for method, url, body in cases:
        r = await _send(client, method, url, h, body)
        assert r.status_code == 403, f"{method.upper()} {url} -> {r.status_code}"


@pytest.mark.asyncio
async def test_member_operates_but_does_not_administer(
    client: AsyncClient, tenant: dict
) -> None:
    """MEMBER is the operator line: run the system, do not reshape it.

    Deleting a queue destroys its job history and rotating the API key issues a
    credential, so both sit above the operator.
    """
    h = _auth(tenant["tokens"]["member"])
    p = tenant["project"]
    q = tenant["queue"]
    pol = tenant["policy"]

    allowed = [
        ("post", f"{API}/queues/{q}/jobs", {"job_type": "x", "payload": {}}, 201),
        ("post", f"{API}/queues/{q}/pause", None, 200),
        ("post", f"{API}/queues/{q}/resume", None, 200),
        ("patch", f"{API}/retry-policies/{pol}", {"max_attempts": 4}, 200),
    ]
    for method, url, body, want in allowed:
        r = await _send(client, method, url, h, body)
        assert r.status_code == want, f"{method.upper()} {url} -> {r.status_code} {r.text}"

    refused = [
        ("delete", f"{API}/queues/{q}", None),
        ("patch", f"{API}/projects/{p}", {"name": "x"}),
        ("delete", f"{API}/projects/{p}", None),
        ("post", f"{API}/projects/{p}/api-key", None),
    ]
    for method, url, body in refused:
        r = await _send(client, method, url, h, body)
        assert r.status_code == 403, f"{method.upper()} {url} -> {r.status_code}"


@pytest.mark.asyncio
async def test_higher_roles_inherit(client: AsyncClient, tenant: dict) -> None:
    """Roles are ranked, not matched. An ADMIN check must be satisfied by an
    OWNER without OWNER being listed anywhere."""
    p = tenant["project"]
    q = tenant["queue"]

    r = await client.post(
        f"{API}/projects/{p}/api-key", headers=_auth(tenant["tokens"]["admin"])
    )
    assert r.status_code == 200, r.text

    r = await client.delete(f"{API}/queues/{q}", headers=_auth(tenant["tokens"]["owner"]))
    assert r.status_code == 204, r.text


# =============================================================================
# Authentication
# =============================================================================


@pytest.mark.asyncio
async def test_protected_routes_reject_bad_tokens(
    client: AsyncClient, tenant: dict
) -> None:
    url = f"{API}/projects/{tenant['project']}/queues"

    assert (await client.get(url)).status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert (await client.get(url, headers=bad)).status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_is_not_an_access_token(client: AsyncClient) -> None:
    """The `type` claim is what stops a long-lived refresh token being replayed
    as an access token against every protected route."""
    suffix = uuid.uuid4().hex[:8]
    r = await client.post(
        f"{API}/auth/register",
        json={
            "email": f"typed-{suffix}@example.com",
            "password": PASSWORD,
            "full_name": "Typed",
            "organization_name": f"Typed Co {suffix}",
        },
    )
    refresh = r.json()["refresh_token"]

    assert (await client.get(f"{API}/orgs", headers=_auth(refresh))).status_code == 401


# =============================================================================
# Role administration
# =============================================================================


@pytest.mark.asyncio
async def test_admin_cannot_grant_a_role_above_their_own(
    client: AsyncClient, tenant: dict
) -> None:
    """The escalation this suite exists to prevent.

    Without the grant rule, an `admin` sets their own row to `owner` — and the
    last-owner guard does not help, because once there are two owners, demoting
    or removing the original is permitted. That is a full takeover of the
    organization by anyone trusted enough to manage members.
    """
    org = tenant["org"]
    admin_h = _auth(tenant["tokens"]["admin"])
    roster = (await client.get(f"{API}/orgs/{org}/members", headers=admin_h)).json()
    by_role = {m["role"]: m["user_id"] for m in roster}

    # Promote self.
    r = await client.patch(
        f"{API}/orgs/{org}/members/{by_role['admin']}",
        headers=admin_h,
        json={"role": "owner"},
    )
    assert r.status_code == 403, r.text

    # Promote someone else as a proxy.
    r = await client.patch(
        f"{API}/orgs/{org}/members/{by_role['viewer']}",
        headers=admin_h,
        json={"role": "owner"},
    )
    assert r.status_code == 403, r.text

    # Invite a fresh owner from outside.
    _, outsider_email = await _register(client, "outsider")
    r = await client.post(
        f"{API}/orgs/{org}/members",
        headers=admin_h,
        json={"email": outsider_email, "role": "owner"},
    )
    assert r.status_code == 403, r.text

    after = (await client.get(f"{API}/orgs/{org}/members", headers=admin_h)).json()
    assert sorted(m["role"] for m in after) == sorted(m["role"] for m in roster)


@pytest.mark.asyncio
async def test_admin_cannot_touch_a_member_who_outranks_them(
    client: AsyncClient, tenant: dict
) -> None:
    """The mirror rule. Blocking upward grants is pointless if an admin can
    instead demote or delete every owner out of the way."""
    org = tenant["org"]
    admin_h = _auth(tenant["tokens"]["admin"])
    roster = (await client.get(f"{API}/orgs/{org}/members", headers=admin_h)).json()
    owner_id = next(m["user_id"] for m in roster if m["role"] == "owner")

    r = await client.patch(
        f"{API}/orgs/{org}/members/{owner_id}",
        headers=admin_h,
        json={"role": "viewer"},
    )
    assert r.status_code == 403, r.text

    r = await client.delete(f"{API}/orgs/{org}/members/{owner_id}", headers=admin_h)
    assert r.status_code == 403, r.text

    after = (await client.get(f"{API}/orgs/{org}/members", headers=admin_h)).json()
    assert any(m["role"] == "owner" for m in after), "the owner was removed"


@pytest.mark.asyncio
async def test_admin_may_still_administer_at_or_below_their_rank(
    client: AsyncClient, tenant: dict
) -> None:
    """The guard must not break the job an admin is there to do."""
    org = tenant["org"]
    admin_h = _auth(tenant["tokens"]["admin"])
    roster = (await client.get(f"{API}/orgs/{org}/members", headers=admin_h)).json()
    viewer_id = next(m["user_id"] for m in roster if m["role"] == "viewer")

    for role in ("member", "admin", "viewer"):
        r = await client.patch(
            f"{API}/orgs/{org}/members/{viewer_id}",
            headers=admin_h,
            json={"role": role},
        )
        assert r.status_code == 200, f"admin -> {role}: {r.status_code} {r.text}"

    _, email = await _register(client, "recruit")
    r = await client.post(
        f"{API}/orgs/{org}/members",
        headers=admin_h,
        json={"email": email, "role": "member"},
    )
    assert r.status_code == 201, r.text
    r = await client.delete(
        f"{API}/orgs/{org}/members/{r.json()['user_id']}", headers=admin_h
    )
    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_last_owner_cannot_be_stranded(client: AsyncClient, tenant: dict) -> None:
    """An organization with no owner is unadministrable — nobody could add
    members, change roles, or delete it."""
    org = tenant["org"]
    owner_h = _auth(tenant["tokens"]["owner"])
    roster = (await client.get(f"{API}/orgs/{org}/members", headers=owner_h)).json()
    owner_id = next(m["user_id"] for m in roster if m["role"] == "owner")

    r = await client.patch(
        f"{API}/orgs/{org}/members/{owner_id}", headers=owner_h, json={"role": "admin"}
    )
    assert r.status_code == 422, r.text

    r = await client.delete(f"{API}/orgs/{org}/members/{owner_id}", headers=owner_h)
    assert r.status_code == 422, r.text
