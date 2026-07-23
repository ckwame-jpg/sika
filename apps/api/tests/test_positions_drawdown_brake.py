"""Tests for Smarter #32 — drawdown brake snapshot on ``GET /positions``.

The portfolio panel polls ``/positions`` every ~15 s. Surfacing the
brake snapshot on the same response lets the UI render a "drawdown
brake active" banner without a separate request or per-recommendation
walk through ``scoring_diagnostics.kelly_sizing``.

Covers:
- Snapshot is populated when bankroll resolution succeeds (default
  settings have ``kelly_sizing_bankroll_dollars=1000``).
- Snapshot is ``None`` (the JSON field is null) when bankroll
  resolution fails (operator zeroed out the setting + Kalshi opt-in
  off).
- ``is_active`` flips true once rolling 7-day PnL drops below the
  default ``-0.05`` threshold.
- The response otherwise round-trips the existing paper/demo/Kalshi
  fields untouched (no regression to bug #28's pagination caps).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_positions_includes_drawdown_brake_snapshot(client, db_session) -> None:
    """Default settings (bankroll=$1000) + no settled rows → snapshot
    is populated with ``is_active=False``."""
    response = client.get("/positions")

    assert response.status_code == 200
    payload = response.json()
    assert "drawdown_brake" in payload
    brake = payload["drawdown_brake"]
    assert brake is not None
    assert brake["bankroll"] == 1000.0
    assert brake["rolling_pnl_dollars"] == 0.0
    assert brake["rolling_pnl_fraction"] == 0.0
    assert brake["brake_multiplier"] == 1.0
    assert brake["threshold"] < 0.0
    assert brake["is_active"] is False


def test_positions_drawdown_brake_activates_after_losing_week(
    client, db_session,
) -> None:
    """A 7-day PnL below the brake threshold → ``is_active=True`` +
    multiplier < 1.0.

    Patches ``compute_rolling_pnl_dollars`` rather than seeding rows
    so the test is deterministic regardless of wall-clock NOW vs.
    the ``settled_at`` window the real query applies. -$55 / $1000
    bankroll = -0.055 fraction (just past the default -0.05
    threshold)."""
    with patch(
        "app.services.kelly_sizing_db.compute_rolling_pnl_dollars",
        return_value=-55.0,
    ):
        response = client.get("/positions")

    assert response.status_code == 200
    brake = response.json()["drawdown_brake"]
    assert brake is not None
    assert brake["rolling_pnl_dollars"] == pytest.approx(-55.0)
    assert brake["rolling_pnl_fraction"] < brake["threshold"]
    assert brake["brake_multiplier"] < 1.0
    assert brake["is_active"] is True


def test_positions_drawdown_brake_is_null_when_no_bankroll(
    client, db_session, monkeypatch,
) -> None:
    """Operator zeroed out ``kelly_sizing_bankroll_dollars`` and the
    Kalshi opt-in is off → resolver returns ``None`` → the JSON
    field is ``null`` so the UI hides the brake panel."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "kelly_sizing_bankroll_dollars", 0.0)
    monkeypatch.setattr(settings, "kelly_sizing_use_kalshi_balance", False)

    response = client.get("/positions")

    assert response.status_code == 200
    assert response.json()["drawdown_brake"] is None


def test_positions_round_trips_existing_fields_with_brake(
    client, db_session,
) -> None:
    """Adding ``drawdown_brake`` must not regress the existing
    paper/demo/Kalshi response shape (bug #28's pagination caps,
    truncation flags, Kalshi snapshot)."""
    response = client.get("/positions")

    assert response.status_code == 200
    payload = response.json()
    for required_key in (
        "paper_positions",
        "demo_orders",
        "kalshi_account",
        "paper_truncated",
        "demo_truncated",
        "drawdown_brake",
    ):
        assert required_key in payload, f"missing {required_key}"
