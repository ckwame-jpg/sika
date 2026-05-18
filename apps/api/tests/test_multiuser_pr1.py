"""Multi-user batch PR 1 — User model + seeding + session + endpoints.

Covers:

- ``seed_users_from_settings`` is idempotent and a no-op in single-tenant
  mode (empty SIKA_USERS)
- Inserting + flagging ``is_kalshi_owner`` syncs from env vars on each
  run (codex pattern 1: state-machine compat — promoting/demoting an
  owner is an explicit overwrite, not additive)
- The legacy bucket is auto-created once any users are configured
- ``GET /me`` returns ``{user: null}`` with no cookie; the chosen
  user once switched
- ``POST /users/switch`` sets the cookie + rejects unknown users +
  rejects impersonation of the legacy bucket
- ``GET /users`` excludes the legacy bucket from the dropdown
- ``POST /users/sign-out`` clears the cookie
- Cookie persists across requests in the test client
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User
from app.services.users import (
    LEGACY_USERNAME,
    list_active_users,
    seed_users_from_settings,
)


def _settings(users: str = "", kalshi_owner: str = "") -> Settings:
    """Build a Settings instance with the multi-user fields populated.
    Bypasses .env loading so the test environment doesn't leak into
    fixture state."""
    return Settings(SIKA_USERS=users, SIKA_KALSHI_OWNER=kalshi_owner)


# -----------------------------------------------------------------------------
# seed_users_from_settings


def test_seed_users_is_noop_in_single_tenant_mode(db_session: Session) -> None:
    """Empty SIKA_USERS → no rows inserted, legacy bucket not created.
    Lets existing deployments boot without any multi-user behavior
    until the operator opts in."""
    summary = seed_users_from_settings(db_session, _settings())
    assert summary == {"inserted": 0, "owner_set": "", "legacy_ensured": 0}
    assert db_session.query(User).count() == 0


def test_seed_users_inserts_new_rows_and_creates_legacy_bucket(
    db_session: Session,
) -> None:
    summary = seed_users_from_settings(
        db_session, _settings(users="chris,canaan")
    )
    assert summary["inserted"] == 2
    assert summary["legacy_ensured"] == 1
    usernames = {u.username for u in db_session.query(User).all()}
    assert usernames == {"chris", "canaan", LEGACY_USERNAME}
    legacy = next(u for u in db_session.query(User).all() if u.username == LEGACY_USERNAME)
    assert legacy.is_legacy_bucket is True


def test_seed_users_is_idempotent(db_session: Session) -> None:
    """Codex pattern 5 (reset edge cases): running the seed twice with
    the same input is a no-op on the second run. Critical for the
    startup hook, which runs every API boot."""
    seed_users_from_settings(db_session, _settings(users="chris,canaan"))
    db_session.commit()
    second = seed_users_from_settings(db_session, _settings(users="chris,canaan"))
    assert second["inserted"] == 0
    assert second["legacy_ensured"] == 0


def test_seed_users_syncs_kalshi_owner_flag(db_session: Session) -> None:
    """Codex pattern 1 (state-machine compat): the owner flag is set
    on the named user and CLEARED on everyone else. A previous
    operator who was the owner can be demoted via the env var alone."""
    seed_users_from_settings(
        db_session, _settings(users="chris,canaan", kalshi_owner="chris")
    )
    db_session.commit()
    chris = db_session.query(User).filter_by(username="chris").one()
    canaan = db_session.query(User).filter_by(username="canaan").one()
    assert chris.is_kalshi_owner is True
    assert canaan.is_kalshi_owner is False

    # Now demote chris and promote canaan.
    seed_users_from_settings(
        db_session, _settings(users="chris,canaan", kalshi_owner="canaan")
    )
    db_session.commit()
    db_session.refresh(chris)
    db_session.refresh(canaan)
    assert chris.is_kalshi_owner is False
    assert canaan.is_kalshi_owner is True


def test_seed_users_ignores_owner_pointing_at_unknown_username(
    db_session: Session,
) -> None:
    """If SIKA_KALSHI_OWNER names someone not in SIKA_USERS, nothing
    is set (rather than erroring). Operator gets a silent no-op on
    the owner flag; the inserted users still land."""
    seed_users_from_settings(
        db_session, _settings(users="chris,canaan", kalshi_owner="ghost")
    )
    db_session.commit()
    owners = [u for u in db_session.query(User).all() if u.is_kalshi_owner]
    assert owners == []


def test_list_active_users_excludes_legacy_bucket(db_session: Session) -> None:
    seed_users_from_settings(db_session, _settings(users="chris,canaan"))
    db_session.commit()
    active = list_active_users(db_session)
    usernames = [u.username for u in active]
    assert "chris" in usernames
    assert "canaan" in usernames
    assert LEGACY_USERNAME not in usernames


# -----------------------------------------------------------------------------
# /me, /users, /users/switch, /users/sign-out endpoints


def _seed_two_users(db_session: Session) -> None:
    seed_users_from_settings(
        db_session, _settings(users="chris,canaan", kalshi_owner="chris")
    )
    db_session.commit()


def test_me_returns_null_without_cookie(client: TestClient, db_session: Session) -> None:
    _seed_two_users(db_session)
    response = client.get("/me")
    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_users_list_returns_active_users_only(
    client: TestClient, db_session: Session
) -> None:
    _seed_two_users(db_session)
    response = client.get("/users")
    assert response.status_code == 200
    rows = response.json()
    usernames = [row["username"] for row in rows]
    assert usernames == ["canaan", "chris"]  # alphabetical
    assert LEGACY_USERNAME not in usernames
    # is_kalshi_owner serialized correctly.
    chris = next(row for row in rows if row["username"] == "chris")
    assert chris["is_kalshi_owner"] is True


def test_switch_user_sets_cookie_and_persists_across_requests(
    client: TestClient, db_session: Session
) -> None:
    _seed_two_users(db_session)
    switch = client.post("/users/switch", json={"username": "canaan"})
    assert switch.status_code == 200
    assert switch.json()["user"]["username"] == "canaan"
    # Cookie persists in the TestClient's cookie jar — subsequent
    # /me hits should return canaan without re-supplying the cookie
    # manually.
    me = client.get("/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "canaan"


def test_switch_user_rejects_unknown_username(
    client: TestClient, db_session: Session
) -> None:
    _seed_two_users(db_session)
    response = client.post("/users/switch", json={"username": "ghost"})
    assert response.status_code == 400
    assert "ghost" in response.json()["detail"].lower()


def test_switch_user_rejects_legacy_bucket(
    client: TestClient, db_session: Session
) -> None:
    """Codex pattern 6 (implicit data shape): the legacy bucket is a
    synthetic identity for historical data, not a real operator. The
    switch endpoint refuses to set the cookie to it."""
    _seed_two_users(db_session)
    response = client.post("/users/switch", json={"username": LEGACY_USERNAME})
    assert response.status_code == 400
    assert "legacy" in response.json()["detail"].lower()


def test_sign_out_clears_the_cookie(
    client: TestClient, db_session: Session
) -> None:
    _seed_two_users(db_session)
    client.post("/users/switch", json={"username": "chris"})
    assert client.get("/me").json()["user"]["username"] == "chris"

    out = client.post("/users/sign-out")
    assert out.status_code == 200
    assert out.json() == {"user": None}
    # The TestClient's cookie jar should now be cleared of sika.userId.
    assert client.get("/me").json() == {"user": None}


def test_me_falls_back_to_null_when_cookie_names_a_deleted_user(
    client: TestClient, db_session: Session
) -> None:
    """Codex pattern 5 (reset edge cases): if the operator drops a
    user from SIKA_USERS, any browser still holding a cookie for that
    username quietly falls back to anonymous rather than 500-ing."""
    _seed_two_users(db_session)
    client.post("/users/switch", json={"username": "canaan"})
    # Drop canaan from the user table (simulates the operator
    # removing them from SIKA_USERS and deleting the row).
    canaan = db_session.query(User).filter_by(username="canaan").one()
    db_session.delete(canaan)
    db_session.commit()
    # The cookie still names canaan, but the middleware finds no row.
    me = client.get("/me")
    assert me.status_code == 200
    assert me.json() == {"user": None}
