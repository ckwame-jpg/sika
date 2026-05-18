"""Multi-user batch PR 5 — in-app user management endpoints + service."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User
from app.services.users import (
    LEGACY_USERNAME,
    create_user,
    delete_user,
    seed_users_from_settings,
)


def test_create_user_inserts_a_row(db_session: Session) -> None:
    user = create_user(db_session, username="canaan")
    db_session.commit()
    assert user.username == "canaan"
    assert user.display_name == "canaan"


def test_create_user_returns_existing_row_on_duplicate(db_session: Session) -> None:
    """Codex pattern 5 (reset edge cases): adding the same username
    twice is a no-op, not an error. Lets the UI show 'already added'
    rather than 409."""
    first = create_user(db_session, username="canaan")
    db_session.commit()
    again = create_user(db_session, username="canaan")
    assert first.id == again.id


def test_create_user_rejects_legacy_bucket_name(db_session: Session) -> None:
    """The legacy username is reserved for the historical-data bucket;
    operators can't claim it via the in-app path."""
    with pytest.raises(ValueError):
        create_user(db_session, username=LEGACY_USERNAME)


def test_create_user_rejects_invalid_username(db_session: Session) -> None:
    """Codex pattern 6 (implicit data shape): only lowercase
    identifiers with letters/digits/underscore/hyphen. Mixed-case input
    is lowercased (so 'Chris' becomes 'chris', accepted); spaces and
    special characters are rejected outright."""
    for bad in ["canaan smith", "ca@an", "", "   "]:
        with pytest.raises(ValueError):
            create_user(db_session, username=bad)


def test_create_user_lowercases_mixed_case_input(db_session: Session) -> None:
    user = create_user(db_session, username="Chris")
    assert user.username == "chris"


def test_delete_user_removes_row(db_session: Session) -> None:
    create_user(db_session, username="canaan")
    db_session.commit()
    assert delete_user(db_session, "canaan") is True
    db_session.commit()
    assert db_session.query(User).filter_by(username="canaan").one_or_none() is None


def test_delete_user_is_idempotent_on_missing(db_session: Session) -> None:
    assert delete_user(db_session, "ghost") is False


def test_delete_user_blocks_legacy_bucket(db_session: Session) -> None:
    seed_users_from_settings(
        db_session, Settings(SIKA_USERS="chris,canaan", SIKA_KALSHI_OWNER="chris")
    )
    db_session.commit()
    with pytest.raises(ValueError):
        delete_user(db_session, LEGACY_USERNAME)


def test_delete_user_blocks_kalshi_owner(db_session: Session) -> None:
    """The owner has env-var creds wired up; deleting them from the
    UI would leave the env vars pointing at a non-existent row.
    Operator must update SIKA_KALSHI_OWNER in .env first."""
    seed_users_from_settings(
        db_session, Settings(SIKA_USERS="chris,canaan", SIKA_KALSHI_OWNER="chris")
    )
    db_session.commit()
    with pytest.raises(ValueError):
        delete_user(db_session, "chris")


# -----------------------------------------------------------------------------
# Endpoint coverage


def test_post_users_creates_and_returns_serialized_row(client: TestClient) -> None:
    response = client.post("/users", json={"username": "canaan"})
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "canaan"
    assert body["display_name"] == "canaan"
    assert body["is_kalshi_owner"] is False


def test_post_users_400s_on_invalid_username(client: TestClient) -> None:
    response = client.post("/users", json={"username": "Chris UPPER"})
    assert response.status_code == 400


def test_delete_users_removes_row(
    client: TestClient, db_session: Session
) -> None:
    client.post("/users", json={"username": "canaan"})
    response = client.delete("/users/canaan")
    assert response.status_code == 200
    assert response.json() == {"deleted": True}


def test_delete_users_is_200_on_missing_user(client: TestClient) -> None:
    response = client.delete("/users/ghost")
    assert response.status_code == 200
    assert response.json() == {"deleted": False}


def test_delete_users_400s_on_kalshi_owner(
    client: TestClient, db_session: Session
) -> None:
    seed_users_from_settings(
        db_session, Settings(SIKA_USERS="chris,canaan", SIKA_KALSHI_OWNER="chris")
    )
    db_session.commit()
    response = client.delete("/users/chris")
    assert response.status_code == 400
