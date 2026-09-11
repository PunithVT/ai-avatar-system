"""
Regression tests for the auth / input-validation hardening pass.

Each test here pins a gap that was live in the codebase:

* Registration accepted ""/1-char passwords, ""/50k-char usernames, and
  treated "User@x.com" and "user@x.com" as different accounts. An empty
  password was the worst case: it registered fine but /login rejects empty
  passwords, so the account was unreachable forever.
* Every resource endpoint used optional auth with a shared `demo-user`
  fallback, so anonymous callers were silently served as that one account.
* "Continue as Guest" mapped every guest onto that same shared row, which
  contradicted the UI's "scoped to this browser session only" promise.
"""

import pytest
from httpx import AsyncClient

VALID_PW = "correct-horse-battery"


# ── registration input validation ────────────────────────────────────────
@pytest.mark.parametrize(
    "password,expected",
    [("", 422), ("a", 422), ("short7c", 422), (VALID_PW, 201)],
)
async def test_password_floor_enforced(client: AsyncClient, password, expected):
    r = await client.post(
        "/api/v1/users/register",
        json={
            "email": f"pw{len(password)}@example.com",
            "username": f"pwuser{len(password)}",
            "password": password,
        },
    )
    assert r.status_code == expected, r.text


@pytest.mark.parametrize(
    "username,expected",
    [
        ("", 422),
        ("ab", 422),
        ("u" * 40, 422),
        ("bad space", 422),
        ("-leading", 422),
        ("ok_user-1", 201),
    ],
)
async def test_username_constraints_enforced(client: AsyncClient, username, expected):
    r = await client.post(
        "/api/v1/users/register",
        json={"email": "un@example.com", "username": username, "password": VALID_PW},
    )
    assert r.status_code == expected, r.text


async def test_registered_account_can_actually_log_in(client: AsyncClient):
    """The empty-password bug made a registered account permanently unusable."""
    await client.post(
        "/api/v1/users/register",
        json={"email": "round@example.com", "username": "roundtrip", "password": VALID_PW},
    )
    r = await client.post(
        "/api/v1/users/login",
        data={"username": "round@example.com", "password": VALID_PW},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]


async def test_email_case_does_not_create_a_second_account(client: AsyncClient):
    first = await client.post(
        "/api/v1/users/register",
        json={"email": "Case@Example.com", "username": "caseone", "password": VALID_PW},
    )
    assert first.status_code == 201
    assert first.json()["email"] == "case@example.com"

    dupe = await client.post(
        "/api/v1/users/register",
        json={"email": "case@example.com", "username": "casetwo", "password": VALID_PW},
    )
    assert dupe.status_code == 409, "differently-cased email must collide, not fork the account"


async def test_login_accepts_the_capitalisation_used_at_signup(client: AsyncClient):
    await client.post(
        "/api/v1/users/register",
        json={"email": "MiXeD@Example.com", "username": "mixeduser", "password": VALID_PW},
    )
    r = await client.post(
        "/api/v1/users/login",
        data={"username": "MiXeD@Example.com", "password": VALID_PW},
    )
    assert r.status_code == 200


# ── anonymous access is refused ──────────────────────────────────────────
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/avatars/"),
        ("get", "/api/v1/sessions/"),
        ("get", "/api/v1/conversations/"),
        ("get", "/api/v1/voices/"),
        ("post", "/api/v1/sessions/create"),
    ],
)
async def test_resource_endpoints_reject_anonymous_callers(client: AsyncClient, method, path):
    r = await getattr(client, method)(path) if method == "get" else await client.post(path, json={})
    assert r.status_code == 401, f"{path} served an unauthenticated caller ({r.status_code})"


# ── guest accounts are real, isolated identities ─────────────────────────
async def test_guest_gets_its_own_identity(client: AsyncClient):
    r = await client.post("/api/v1/users/guest")
    assert r.status_code == 201
    token = r.json()["access_token"]

    me = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["is_guest"] is True
    assert body["id"].startswith("guest-")


async def test_two_guests_are_isolated_from_each_other(client: AsyncClient):
    """The shared demo-user fallback let any guest read any other guest's data."""
    a = (await client.post("/api/v1/users/guest")).json()["access_token"]
    b = (await client.post("/api/v1/users/guest")).json()["access_token"]

    id_a = (await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {a}"})).json()[
        "id"
    ]
    id_b = (await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {b}"})).json()[
        "id"
    ]
    assert id_a != id_b, "guests must not share one server-side account"


async def test_guest_account_cannot_be_logged_into(client: AsyncClient):
    """A guest row has an unusable password hash, so it has no login surface."""
    token = (await client.post("/api/v1/users/guest")).json()["access_token"]
    email = (
        await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    ).json()["email"]

    for attempt in ("*", "", "guest", VALID_PW):
        r = await client.post("/api/v1/users/login", data={"username": email, "password": attempt})
        assert r.status_code in (401, 422), f"password {attempt!r} was accepted for a guest"


async def test_guest_can_be_disabled(client: AsyncClient, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "GUEST_ACCOUNTS_ENABLED", False)
    r = await client.post("/api/v1/users/guest")
    assert r.status_code == 403


# ── guest retention sweep ────────────────────────────────────────────────
async def test_guest_retention_sweep_deletes_only_idle_guests(db_session, monkeypatch):
    """
    Guests are real rows, so without a reaper the users table grows by one
    per visitor forever. The sweep must delete idle guests, spare active
    ones, and never touch registered accounts.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from app.api.v1.users import get_password_hash
    from app.config import settings
    from app.models import Session as SessionModel
    from app.models import User

    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=settings.GUEST_RETENTION_HOURS + 5)

    idle_guest = User(
        id="guest-idle",
        email="g1@guests.example.com",
        username="guestidle",
        hashed_password="*",
        is_guest=True,
        created_at=stale,
    )
    active_guest = User(
        id="guest-active",
        email="g2@guests.example.com",
        username="guestactive",
        hashed_password="*",
        is_guest=True,
        created_at=stale,
    )
    registered = User(
        id="real-1",
        email="real@example.com",
        username="realuser",
        hashed_password=get_password_hash(VALID_PW),
        is_guest=False,
        created_at=stale,
    )
    db_session.add_all([idle_guest, active_guest, registered])
    await db_session.commit()

    # The active guest has a recent session; the idle one has none at all.
    avatar_owner = active_guest.id
    from app.models import Avatar

    db_session.add(
        Avatar(id="av-1", user_id=avatar_owner, name="a", image_url="u", s3_key="k", status="ready")
    )
    await db_session.commit()
    db_session.add(
        SessionModel(
            id="sess-1", user_id=active_guest.id, avatar_id="av-1", status="active", started_at=now
        )
    )
    await db_session.commit()

    # Exercise the same selection logic the Celery task runs, against the
    # test session (the task opens its own engine, which the in-memory test
    # DB does not share).
    cutoff = now - timedelta(hours=settings.GUEST_RETENTION_HOURS)
    last_activity = (
        select(
            User.id.label("uid"),
            func.coalesce(
                func.max(func.coalesce(SessionModel.ended_at, SessionModel.started_at)),
                User.created_at,
            ).label("seen"),
        )
        .outerjoin(SessionModel, SessionModel.user_id == User.id)
        .where(User.is_guest.is_(True))
        .group_by(User.id, User.created_at)
        .subquery()
    )
    doomed = set(
        (await db_session.execute(select(last_activity.c.uid).where(last_activity.c.seen < cutoff)))
        .scalars()
        .all()
    )

    assert "guest-idle" in doomed, "an idle guest must be reaped"
    assert "guest-active" not in doomed, "a guest with a live session must survive"
    assert "real-1" not in doomed, "registered accounts must never be reaped"
