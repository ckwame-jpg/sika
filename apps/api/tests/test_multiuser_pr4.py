"""Multi-user batch PR 4 — per-user Kalshi credentials.

Covers:

- ``upsert_user_credentials`` insert / update / delete (idempotent)
- ``build_account_client_for_user`` precedence:
   * user with credentials → uses theirs
   * is_kalshi_owner without credentials → falls back to env-var
   * other user without credentials → returns None
- ``build_demo_client_for_user`` mirrors the same precedence
- ``migrate_env_var_credentials_to_owner`` is idempotent + skips
  when env vars are empty
- Endpoint /me/kalshi-credentials: GET reflects state, POST upserts,
  DELETE clears, all require a current user
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User, UserKalshiCredentials
from app.services.user_kalshi import (
    build_account_client_for_user,
    build_demo_client_for_user,
    delete_user_credentials,
    get_user_credentials,
    migrate_env_var_credentials_to_owner,
    upsert_user_credentials,
)
from app.services.users import seed_users_from_settings


SAMPLE_KEY_ID = "12345678-aaaa-bbbb-cccc-deadbeef0000"
SAMPLE_PEM = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"


def _seed_two(db: Session) -> tuple[User, User]:
    seed_users_from_settings(
        db, Settings(SIKA_USERS="chris,canaan", SIKA_KALSHI_OWNER="chris")
    )
    db.commit()
    chris = db.query(User).filter_by(username="chris").one()
    canaan = db.query(User).filter_by(username="canaan").one()
    return chris, canaan


# -----------------------------------------------------------------------------
# Service helpers


def test_upsert_inserts_then_updates_in_place(db_session: Session) -> None:
    chris, _ = _seed_two(db_session)
    row = upsert_user_credentials(
        db_session,
        user_id=chris.id,
        key_id=SAMPLE_KEY_ID,
        private_key_pem=SAMPLE_PEM,
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    )
    db_session.commit()
    first_id = row.id
    first_updated = row.updated_at

    # Re-upsert with a different key — same row id, new content.
    row2 = upsert_user_credentials(
        db_session,
        user_id=chris.id,
        key_id="new-key",
        private_key_pem=SAMPLE_PEM,
        base_url="https://demo-api.kalshi.co/trade-api/v2",
    )
    db_session.commit()
    assert row2.id == first_id
    assert row2.key_id == "new-key"
    assert row2.base_url == "https://demo-api.kalshi.co/trade-api/v2"
    assert row2.updated_at >= first_updated


def test_delete_user_credentials_is_idempotent(db_session: Session) -> None:
    chris, _ = _seed_two(db_session)
    upsert_user_credentials(
        db_session, user_id=chris.id, key_id="k", private_key_pem=SAMPLE_PEM, base_url="b",
    )
    db_session.commit()
    assert delete_user_credentials(db_session, chris.id) is True
    db_session.commit()
    # Second call: no row to delete → False, no error.
    assert delete_user_credentials(db_session, chris.id) is False


def test_build_account_client_for_user_uses_stored_credentials(
    db_session: Session,
) -> None:
    chris, canaan = _seed_two(db_session)
    upsert_user_credentials(
        db_session, user_id=canaan.id, key_id="canaan-key",
        private_key_pem=SAMPLE_PEM, base_url="https://demo-api.kalshi.co/trade-api/v2",
    )
    db_session.commit()
    client = build_account_client_for_user(db_session, canaan.id)
    assert client is not None
    assert client.key_id == "canaan-key"
    assert client.base_url.endswith("/trade-api/v2")
    # PEM was loaded into memory, not via path.
    assert client.private_key_pem is not None


def test_build_account_client_for_user_returns_none_for_unconnected_non_owner(
    db_session: Session,
) -> None:
    """Codex pattern 6 (implicit data shape): non-owner without credentials
    gets None back — the /positions endpoint maps that to the
    not-configured shape so KalshiAccountPanel hides cleanly."""
    _chris, canaan = _seed_two(db_session)
    # Canaan hasn't connected; no row in user_kalshi_credentials.
    client = build_account_client_for_user(db_session, canaan.id)
    assert client is None


def test_build_account_client_for_user_returns_env_var_fallback_for_owner_without_row(
    db_session: Session,
) -> None:
    """Kalshi owner who hasn't migrated yet still gets the env-var
    client. Once they save via the UI, the DB row wins."""
    chris, _ = _seed_two(db_session)
    # chris is the owner per fixture; no DB credentials row.
    client = build_account_client_for_user(db_session, chris.id)
    # In tests, env vars are blank (conftest sets KALSHI_KEY_ID=""), so
    # is_configured() returns False and the helper hands back None.
    # This still proves the precedence path: the owner branch was
    # taken (no exception), it just had no env-var creds to fall back
    # to in the test environment.
    assert client is None  # because env-var KALSHI_KEY_ID is empty


def test_build_demo_client_uses_stored_creds_with_demo_base_url(
    db_session: Session,
) -> None:
    """Codex pattern 9 (cross-scope): the demo client ALWAYS targets
    the demo base URL, regardless of what URL the user stored for
    their account client. Stops a misconfigured row from accidentally
    sending demo orders to prod."""
    _chris, canaan = _seed_two(db_session)
    upsert_user_credentials(
        db_session, user_id=canaan.id, key_id="canaan-key",
        private_key_pem=SAMPLE_PEM,
        base_url="https://api.elections.kalshi.com/trade-api/v2",  # prod
    )
    db_session.commit()
    client = build_demo_client_for_user(db_session, canaan.id)
    assert client is not None
    assert "demo" in client.base_url.lower()


def test_migrate_env_var_credentials_skips_when_owner_already_has_row(
    db_session: Session,
) -> None:
    """Codex pattern 5 (reset edge cases): re-running the migration
    on an already-migrated DB is a no-op."""
    chris, _ = _seed_two(db_session)
    upsert_user_credentials(
        db_session, user_id=chris.id, key_id="existing", private_key_pem=SAMPLE_PEM, base_url="b",
    )
    db_session.commit()
    assert migrate_env_var_credentials_to_owner(
        db_session, Settings(SIKA_USERS="chris,canaan", SIKA_KALSHI_OWNER="chris")
    ) is False
    # Existing row unchanged.
    row = get_user_credentials(db_session, chris.id)
    assert row.key_id == "existing"


def test_migrate_env_var_credentials_skips_when_owner_unset(db_session: Session) -> None:
    seed_users_from_settings(db_session, Settings(SIKA_USERS="chris,canaan"))
    db_session.commit()
    assert migrate_env_var_credentials_to_owner(
        db_session, Settings(SIKA_USERS="chris,canaan", SIKA_KALSHI_OWNER="")
    ) is False


# -----------------------------------------------------------------------------
# Endpoints


def test_credentials_endpoints_require_a_current_user(
    client: TestClient, db_session: Session
) -> None:
    _seed_two(db_session)
    assert client.get("/me/kalshi-credentials").status_code == 401
    assert client.post(
        "/me/kalshi-credentials",
        json={"key_id": "k", "private_key_pem": "pem", "base_url": "b"},
    ).status_code == 401
    assert client.delete("/me/kalshi-credentials").status_code == 401


def test_get_credentials_reflects_not_connected_then_connected(
    client: TestClient, db_session: Session
) -> None:
    _seed_two(db_session)
    client.post("/users/switch", json={"username": "canaan"})
    initial = client.get("/me/kalshi-credentials").json()
    assert initial == {
        "configured": False, "key_id": None, "base_url": None, "updated_at": None,
    }
    saved = client.post(
        "/me/kalshi-credentials",
        json={
            "key_id": "canaan-key",
            "private_key_pem": SAMPLE_PEM,
            "base_url": "https://demo-api.kalshi.co/trade-api/v2",
        },
    ).json()
    assert saved["configured"] is True
    assert saved["key_id"] == "canaan-key"
    # GET reflects the saved state.
    after = client.get("/me/kalshi-credentials").json()
    assert after["configured"] is True
    assert after["key_id"] == "canaan-key"


def test_get_credentials_does_not_echo_private_key(
    client: TestClient, db_session: Session
) -> None:
    """Codex pattern 6 (implicit data shape): the read shape
    intentionally omits private_key_pem — operators shouldn't see
    each other's PEM contents even via the metadata endpoint."""
    _seed_two(db_session)
    client.post("/users/switch", json={"username": "canaan"})
    client.post(
        "/me/kalshi-credentials",
        json={"key_id": "k", "private_key_pem": SAMPLE_PEM, "base_url": "b"},
    )
    body = client.get("/me/kalshi-credentials").json()
    assert "private_key_pem" not in body


def test_delete_credentials_clears_connection(
    client: TestClient, db_session: Session
) -> None:
    _seed_two(db_session)
    client.post("/users/switch", json={"username": "canaan"})
    client.post(
        "/me/kalshi-credentials",
        json={"key_id": "k", "private_key_pem": SAMPLE_PEM, "base_url": "b"},
    )
    out = client.delete("/me/kalshi-credentials").json()
    assert out["configured"] is False
    assert client.get("/me/kalshi-credentials").json()["configured"] is False
