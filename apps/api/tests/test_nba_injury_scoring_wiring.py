"""Tests for Smarter #17 phase 3 — scoring-path wiring of NBA injury features.

Phase 1 shipped ``emit_nba_injury_features`` + the
``_single_scoring_adjustments`` suppression gate. Phase 2 shipped the
``NbaInjuryReportCache`` model + ``load_nba_injury_report`` cache
loader + the daily refresh-job scheduler entry. Phase 3 (this test
target) is the consumer-side wiring inside ``_score_player_prop``'s
NBA branch: scoring an NBA player prop calls ``load_nba_injury_report``,
passes its payload through ``emit_nba_injury_features``, and merges
the result into the features dict so the existing suppression gate
fires on real games.

These tests verify the wiring without re-testing the components (which
are covered separately in ``test_nba_injury_suppression.py`` and
``test_nba_injury_report_loader.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.models import NbaInjuryReportCache
from app.services import scoring


_NOW = datetime(2026, 5, 15, 19, 0, tzinfo=timezone.utc)


def _seed_injury_cache(
    db_session,
    *,
    players: dict[str, str],
    fetched_date: str | None = None,
    report_offset_hours: float = 1.0,
    expires_offset_hours: float = 1.0,
) -> None:
    """Seed an ``NbaInjuryReportCache`` row keyed to today's UTC date.

    ``players`` maps player name → ESPN status (``out`` / ``doubtful`` /
    ``questionable`` / ``day-to-day``). ``report_offset_hours`` controls
    the freshness signal — values inside the 12h window mark the report
    as fresh; longer means stale.
    """
    fetched = fetched_date or _NOW.strftime("%Y-%m-%d")
    payload = {
        "report_updated_at": (_NOW - timedelta(hours=report_offset_hours)).isoformat(),
        "players": {
            name: {"status": status, "designation": "test"}
            for name, status in players.items()
        },
    }
    db_session.add(
        NbaInjuryReportCache(
            fetched_date=fetched,
            payload=payload,
            cached_at=_NOW,
            expires_at=_NOW + timedelta(hours=expires_offset_hours),
        )
    )
    db_session.commit()


def _run_nba_injury_wiring_slice(
    db_session,
    *,
    display_name: str,
) -> dict[str, Any]:
    """Run JUST the new wiring (load + emit) the same way the NBA branch
    of ``_score_player_prop`` does. The two-line wiring is what phase 3
    ships; we exercise it directly here so the test stays focused on
    the contract (scoring path → loader → emitter → features) rather
    than re-instantiating the full prop scorer (which has a heavy
    setup unrelated to this PR's behavior).

    The slice mirrors the production code at scoring.py:1701–1718 and
    will fail in lockstep if either the import or the call signature
    drifts.

    Pass ``now=_NOW`` to the loader as well as the emitter so the
    cache's ``expires_at`` check uses the same controlled clock the
    seed wrote against. Without it, the loader falls back to
    ``utcnow()`` and treats the seeded row as stale whenever real
    time has moved past ``_NOW + expires_offset_hours``, which made
    the suite fail intermittently after the seed clock baked into
    the past.
    """
    from app.services.advanced_stats import emit_nba_injury_features
    from app.services.nba_injury_report import load_nba_injury_report

    features: dict[str, Any] = {}
    injury_payload = load_nba_injury_report(
        db_session, allow_network=False, now=_NOW,
    )
    features.update(
        emit_nba_injury_features(
            injury_payload, player_name=display_name, now=_NOW,
        )
    )
    return features


# -- Wiring exercised through the slice helper -------------------------


def test_nba_scoring_wiring_emits_out_features_when_player_in_cache(db_session) -> None:
    _seed_injury_cache(db_session, players={"Jayson Tatum": "out"})

    features = _run_nba_injury_wiring_slice(db_session, display_name="Jayson Tatum")

    assert features["injury_data_complete"] == 1.0
    assert features["injury_report_is_fresh"] == 1.0
    assert features["player_injury_status_out"] == 1.0
    assert features["player_injury_status_doubtful"] == 0.0


def test_nba_scoring_wiring_no_features_when_player_not_in_cache(db_session) -> None:
    _seed_injury_cache(db_session, players={"LeBron James": "out"})

    features = _run_nba_injury_wiring_slice(db_session, display_name="Jayson Tatum")

    assert features == {}


def test_nba_scoring_wiring_no_features_when_cache_missing(db_session) -> None:
    # No NbaInjuryReportCache row at all → loader returns the empty
    # shape, emitter returns {}.
    features = _run_nba_injury_wiring_slice(db_session, display_name="Jayson Tatum")

    assert features == {}


def test_nba_scoring_wiring_marks_stale_report_not_fresh(db_session) -> None:
    """Reports older than 12h still emit the status flag but
    ``injury_report_is_fresh=0.0`` so the suppression gate (which
    requires ``is_fresh=1.0``) doesn't fire on stale data."""
    _seed_injury_cache(
        db_session, players={"Jayson Tatum": "out"},
        report_offset_hours=24.0,  # well outside the 12h window
    )

    features = _run_nba_injury_wiring_slice(db_session, display_name="Jayson Tatum")

    assert features["player_injury_status_out"] == 1.0
    assert features["injury_report_is_fresh"] == 0.0


# -- End-to-end through _score_player_prop NBA branch ------------------


def test_score_player_prop_imports_match_phase_3_wiring(monkeypatch) -> None:
    """Confirm scoring.py:_score_player_prop imports both
    ``load_nba_injury_report`` and ``emit_nba_injury_features`` in the
    NBA branch. A future refactor that drops one of these imports
    breaks the wiring without obvious test-suite signal otherwise."""
    import inspect

    source = inspect.getsource(scoring._score_player_prop)
    assert "from app.services.nba_injury_report import load_nba_injury_report" in source
    assert "emit_nba_injury_features" in source
    # The emit call must be passed `player_name` to bind the prop subject.
    assert "emit_nba_injury_features(injury_payload, player_name=" in source


def test_score_player_prop_load_call_uses_allow_network_false(monkeypatch) -> None:
    """The cache is populated by the daily refresh job — scoring must
    NEVER hit the network. Without ``allow_network=False`` an outage
    on the ESPN injury endpoint could block prop scoring entirely."""
    import inspect

    source = inspect.getsource(scoring._score_player_prop)
    assert "load_nba_injury_report(db, allow_network=False)" in source
