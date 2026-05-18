"""Bug #235 — assert PATCH ``/ops/models/readiness/settings`` is fast.

The previous version returned the full ``ModelReadinessSummaryRead``
payload, which forced ``build_model_readiness_summary(db)`` (~22s in
production) to run inside the request handler. That blew past the
15s client timeout in ``apps/web/lib/api.ts`` and surfaced a
misleading "request timed out" overlay on every settings-page click
even though the write itself completed in milliseconds.

These tests pin the contract the fix relies on:

1. The response shape is the lightweight ``{"applied": true}`` ack
   (not ``ModelReadinessSummaryRead``).
2. The PATCH does NOT invoke ``build_model_readiness_summary`` — we
   patch the helper to raise if the route calls it.
3. The PATCH returns in well under 2s for typical partial-PATCH
   payloads (narrator toggle, depth select, sportsbook knobs).

The slow summary still runs on the GET endpoint — that path is
unchanged. SWR ``mutate(keys.modelReadinessSummary)`` after the PATCH
re-fetches the GET in the background; the user sees the chip flip
instantly via cache update + revalidate.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import OperatorSetting
from app.services.operator_settings import (
    DEFAULT_PICK_HISTORY_N,
    effective_narrator_enabled,
    effective_pick_history_default_n,
)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# -- Response shape ----------------------------------------------------


def test_patch_returns_lightweight_applied_ack(client) -> None:
    """The PATCH must return the new lightweight ack and nothing else.

    The previous shape (full ``ModelReadinessSummaryRead``) leaked
    summary fields into the response — keeping the new shape minimal
    lets the route avoid the slow summary build entirely."""
    response = client.patch(
        "/ops/models/readiness/settings",
        json={"narrator_enabled": True},
    )
    assert response.status_code == 200
    assert response.json() == {"applied": True}


def test_patch_returns_applied_ack_when_payload_is_empty(client) -> None:
    """An empty PATCH (no fields to update) is still a valid call —
    it's a no-op write that still acknowledges. The settings page
    relies on this being fast and idempotent."""
    response = client.patch(
        "/ops/models/readiness/settings",
        json={},
    )
    assert response.status_code == 200
    assert response.json() == {"applied": True}


# -- No summary build inside the handler -------------------------------


def test_patch_does_not_call_build_model_readiness_summary(client, monkeypatch) -> None:
    """The whole point of the split is that PATCH never calls the slow
    summary helper. If the route is ever wired back to return the full
    summary, this test trips immediately."""

    def _boom(*_args, **_kwargs):  # pragma: no cover - failure path only
        raise AssertionError(
            "PATCH /ops/models/readiness/settings must not invoke "
            "build_model_readiness_summary (Bug #235)."
        )

    # Patch the symbol where the route imports it so any call inside
    # the handler trips the assertion.
    monkeypatch.setattr(
        "app.api.routes.build_model_readiness_summary",
        _boom,
    )

    response = client.patch(
        "/ops/models/readiness/settings",
        json={"pick_history_default_n": 10},
    )
    assert response.status_code == 200
    assert response.json() == {"applied": True}


# -- Latency ----------------------------------------------------------


def test_patch_returns_in_under_two_seconds_for_partial_payloads(client, db_session) -> None:
    """Tight ceiling so a regression that re-introduces the slow
    summary build inside the handler trips loudly. The local test
    process has no production-sized fixture data; 2s is well over
    what the partial-PATCH write itself needs.

    The 22s production lag came from summary aggregation queries that
    run on the GET path; this test only protects the PATCH path."""
    payloads = [
        {"narrator_enabled": True},
        {"pick_history_default_n": 10},
        {"sportsbook_disagreement_threshold": 0.12},
        {"sportsbook_disagreement_min_book_count": 4},
        {
            "narrator_enabled": False,
            "pick_history_default_n": 5,
            "sportsbook_disagreement_threshold": 0.15,
        },
    ]
    for payload in payloads:
        start = time.monotonic()
        response = client.patch(
            "/ops/models/readiness/settings",
            json=payload,
        )
        elapsed = time.monotonic() - start
        assert response.status_code == 200, payload
        assert response.json() == {"applied": True}, payload
        assert elapsed < 2.0, (
            f"PATCH {payload} took {elapsed:.2f}s — Bug #235 ceiling is 2s. "
            "Did the route start calling build_model_readiness_summary again?"
        )


# -- Writes still persist ---------------------------------------------


def test_patch_persists_pick_history_depth_visible_on_get(client) -> None:
    """The PATCH is fast because it no longer echoes the summary, but
    the write itself still commits — verify by GETting the canonical
    summary after."""
    response = client.patch(
        "/ops/models/readiness/settings",
        json={"pick_history_default_n": 20},
    )
    assert response.status_code == 200

    summary = client.get("/ops/models/readiness")
    assert summary.status_code == 200
    assert summary.json()["pick_history_default_n"] == 20


def test_patch_persists_narrator_toggle_visible_on_get(client) -> None:
    response = client.patch(
        "/ops/models/readiness/settings",
        json={"narrator_enabled": True},
    )
    assert response.status_code == 200

    summary = client.get("/ops/models/readiness")
    assert summary.status_code == 200
    assert summary.json()["narrator_enabled"] is True


def test_patch_persists_settings_in_db_row(client, db_session) -> None:
    """Sanity check that the write actually hits the OperatorSetting
    table — the lightweight response shape doesn't tell us the write
    happened, so the DB is the authoritative check."""
    # Baseline: no rows, so effective values return defaults.
    assert effective_pick_history_default_n(db_session) == DEFAULT_PICK_HISTORY_N
    assert effective_narrator_enabled(db_session) is False

    response = client.patch(
        "/ops/models/readiness/settings",
        json={"pick_history_default_n": 10, "narrator_enabled": True},
    )
    assert response.status_code == 200
    assert response.json() == {"applied": True}

    # Both writes landed.
    assert effective_pick_history_default_n(db_session) == 10
    assert effective_narrator_enabled(db_session) is True
    # And the rows are real (not a default-fallback artifact).
    persisted = {
        row.key
        for row in db_session.scalars(select(OperatorSetting)).all()
    }
    assert "pick_history_default_n" in persisted
    assert "narrator_enabled" in persisted
