"""Operator settings — pick-history N default.

The trade-ticket pick-history strip reads ``pick_history_default_n`` from
the operator settings store on mount. Per-pick toggles in the strip
header override this for the active ticket session only.
"""

from __future__ import annotations

import pytest

from app.services.operator_settings import (
    DEFAULT_PICK_HISTORY_N,
    PICK_HISTORY_N_MAX,
    PICK_HISTORY_N_MIN,
    effective_pick_history_default_n,
    set_pick_history_default_n,
)


def test_effective_default_falls_back_when_no_row(db_session):
    assert effective_pick_history_default_n(db_session) == DEFAULT_PICK_HISTORY_N


def test_effective_default_returns_stored_value(db_session):
    set_pick_history_default_n(db_session, 10)
    db_session.commit()
    assert effective_pick_history_default_n(db_session) == 10


def test_set_default_clamps_to_valid_range(db_session):
    set_pick_history_default_n(db_session, PICK_HISTORY_N_MAX)
    db_session.commit()
    assert effective_pick_history_default_n(db_session) == PICK_HISTORY_N_MAX


def test_set_default_rejects_out_of_range(db_session):
    with pytest.raises(ValueError):
        set_pick_history_default_n(db_session, PICK_HISTORY_N_MIN - 1)
    with pytest.raises(ValueError):
        set_pick_history_default_n(db_session, PICK_HISTORY_N_MAX + 1)


def test_set_default_rejects_non_int(db_session):
    with pytest.raises(ValueError):
        set_pick_history_default_n(db_session, "five")  # type: ignore[arg-type]


def test_default_falls_back_when_stored_value_is_corrupted(db_session):
    """If somebody hand-edits the JSON to something nonsensical, we
    silently fall back to DEFAULT_PICK_HISTORY_N rather than crash."""
    from app.models import OperatorSetting
    from app.services.operator_settings import PICK_HISTORY_DEFAULT_N_KEY

    row = OperatorSetting(key=PICK_HISTORY_DEFAULT_N_KEY, value={"n": 99})
    db_session.add(row)
    db_session.commit()

    assert effective_pick_history_default_n(db_session) == DEFAULT_PICK_HISTORY_N


def test_readiness_settings_endpoint_round_trips_pick_history_default_n(client):
    response = client.patch(
        "/ops/models/readiness/settings",
        json={
            "ml_serving_mode": "heuristic",
            "enqueue_shadow_backfill": False,
            "pick_history_default_n": 10,
        },
    )
    assert response.status_code == 200
    assert response.json()["pick_history_default_n"] == 10


def test_readiness_settings_endpoint_omits_pick_history_default_n_means_no_change(client):
    # First pin a non-default
    pinned = client.patch(
        "/ops/models/readiness/settings",
        json={
            "ml_serving_mode": "heuristic",
            "enqueue_shadow_backfill": False,
            "pick_history_default_n": 20,
        },
    )
    assert pinned.json()["pick_history_default_n"] == 20

    # Then PATCH without it — the pinned value should survive.
    response = client.patch(
        "/ops/models/readiness/settings",
        json={"ml_serving_mode": "heuristic", "enqueue_shadow_backfill": False},
    )
    assert response.status_code == 200
    assert response.json()["pick_history_default_n"] == 20


def test_readiness_settings_endpoint_422_on_out_of_range_n(client):
    response = client.patch(
        "/ops/models/readiness/settings",
        json={
            "ml_serving_mode": "heuristic",
            "enqueue_shadow_backfill": False,
            "pick_history_default_n": 999,
        },
    )
    assert response.status_code == 422
