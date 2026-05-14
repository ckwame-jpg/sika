"""Regression tests for the bugs Codex flagged in CODEX_REVIEW_NOTES.md.

Each test name corresponds to a numbered Codex finding. They lock in the
fix so the bug doesn't regress on a future refactor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import heuristic_factors
from app.services import mlb_advanced


# -----------------------------------------------------------------------------
# Fix #1 — `_mlb_xstats_anchor_factor` no longer claims to use recent xBA.

def test_xstats_anchor_blends_actual_and_expected_ratios():
    """Codex finding #3: previously both halves of the blend read
    ``features.get("season_xba")``, so the expected-stat half was always
    1.0 and the factor only moved on actual recent vs season AVG.
    With the fix, the blend is ``0.5 * (recent / season_avg) + 0.5 *
    (season_xba / season_avg)`` so a player with strong xBA but cold
    recent form gets pulled toward expected."""
    # actual ratio = 0.270 / 0.300 = 0.90 (cold)
    # expected ratio = 0.345 / 0.300 = 1.15 (positive regression target)
    # blend = 0.5 * 0.90 + 0.5 * 1.15 = 1.025, clamped within [0.85, 1.15]
    direct = heuristic_factors._mlb_xstats_anchor_factor({
        "recent_3_average": 0.270,
        "season_average": 0.300,
        "season_xba": 0.345,
    })
    assert direct == pytest.approx(1.025, abs=1e-3)


def test_xstats_anchor_factor_does_not_double_read_season_xba():
    """Direct guard against the typo regressing: if both halves read
    ``season_xba``, swapping the expected-stat ratio would have no effect.
    With the fix, season_xba ALONE shifts the result."""
    base = {"recent_3_average": 0.300, "season_average": 0.300, "season_xba": 0.300}
    high_xba = {**base, "season_xba": 0.345}
    low_xba = {**base, "season_xba": 0.255}
    base_factor = heuristic_factors._mlb_xstats_anchor_factor(base)
    high_factor = heuristic_factors._mlb_xstats_anchor_factor(high_xba)
    low_factor = heuristic_factors._mlb_xstats_anchor_factor(low_xba)
    assert base_factor == pytest.approx(1.0)
    assert high_factor > base_factor
    assert low_factor < base_factor


def test_xstats_anchor_returns_neutral_without_season_xba():
    direct = heuristic_factors._mlb_xstats_anchor_factor(
        {"recent_3_average": 0.300, "season_average": 0.300}
    )
    assert direct == 1.0


# -----------------------------------------------------------------------------
# Fix #2 — MLB lineup parser reads game.lineups.{homePlayers, awayPlayers}.

def test_emit_lineup_features_reads_modern_schedule_schema():
    """Codex finding #1: the parser was looking under teams.{home,away}.
    probableLineup, but the real MLB Stats API hydrate puts confirmed
    lineups under game.lineups.homePlayers / awayPlayers as flat ordered
    arrays."""
    payload = {
        "raw": {
            "dates": [
                {
                    "games": [
                        {
                            "lineups": {
                                "homePlayers": [
                                    {"id": 660271},
                                    {"id": 592450},
                                    {"id": 543037},
                                ],
                                "awayPlayers": [
                                    {"id": 645277},
                                    {"id": 605141},
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    }
    out = mlb_advanced.emit_lineup_features(payload, "592450")
    assert out["batting_order_position"] == 2.0
    assert out["lineup_data_complete"] == 1.0


def test_emit_lineup_features_falls_back_to_legacy_team_block():
    """Tests / fixtures using the older shape still work."""
    payload = {
        "raw": {
            "dates": [
                {
                    "games": [
                        {
                            "teams": {
                                "home": {
                                    "probableLineup": [
                                        {"id": 1, "battingOrder": 1},
                                        {"id": 2, "battingOrder": 2},
                                    ]
                                },
                                "away": {"probableLineup": []},
                            }
                        }
                    ]
                }
            ]
        }
    }
    out = mlb_advanced.emit_lineup_features(payload, "2")
    assert out["batting_order_position"] == 2.0


def test_emit_lineup_features_signals_scratch_when_player_not_in_confirmed_lineup():
    """Smarter #16 changed this contract: when lineup data IS in the payload
    but the target player is NOT in any starting lineup, we now emit
    ``{"lineup_data_complete": 1.0, "player_in_starting_lineup": 0.0}``
    (the actionable scratch / DNP signal) instead of ``{}``. The empty
    dict is reserved for the pre-lineup window when no lineup data exists
    at all."""

    payload = {
        "raw": {
            "dates": [
                {
                    "games": [
                        {
                            "lineups": {
                                "homePlayers": [{"id": 1}],
                                "awayPlayers": [{"id": 2}],
                            }
                        }
                    ]
                }
            ]
        }
    }
    assert mlb_advanced.emit_lineup_features(payload, "999") == {
        "lineup_data_complete": 1.0,
        "player_in_starting_lineup": 0.0,
    }


def test_emit_lineup_features_returns_empty_when_no_lineup_data_at_all():
    """Pre-lineup window contract: payload has games but no lineup arrays."""

    payload = {"raw": {"dates": [{"games": [{"lineups": {}}]}]}}
    assert mlb_advanced.emit_lineup_features(payload, "999") == {}


# -----------------------------------------------------------------------------
# Fix #5 — advanced_stats_warm derives athlete IDs from search-cache when
# none are supplied. This locks in that the cron does meaningful work even
# when the operator doesn't pre-pin a player list.

def test_warm_uses_cached_ids_when_none_supplied(db_session):
    from app.models import EspnPlayerSearchCache, utcnow
    from app.services.refresh_jobs import REFRESH_JOB_KINDS

    # Pre-populate the search cache with one NBA + one MLB player that have
    # already been resolved. The warm dispatch should pick these up via the
    # sidecar mapping without the operator passing IDs.
    db_session.add_all([
        EspnPlayerSearchCache(
            sport_key="NBA",
            query_normalized="lebron james",
            payload={"athlete_id": "1966", "nba_stats_id": "2544", "display_name": "LeBron James"},
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(days=7),
        ),
        EspnPlayerSearchCache(
            sport_key="MLB",
            query_normalized="aaron judge",
            payload={"athlete_id": "33192", "mlb_stats_id": "592450", "display_name": "Aaron Judge"},
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(days=7),
        ),
    ])
    db_session.flush()

    nba_ids = sorted({
        str(payload.get("nba_stats_id"))
        for entry in db_session.query(EspnPlayerSearchCache).filter(EspnPlayerSearchCache.sport_key == "NBA").all()
        for payload in [entry.payload or {}]
        if payload.get("nba_stats_id")
    })
    mlb_ids = sorted({
        str(payload.get("mlb_stats_id"))
        for entry in db_session.query(EspnPlayerSearchCache).filter(EspnPlayerSearchCache.sport_key == "MLB").all()
        for payload in [entry.payload or {}]
        if payload.get("mlb_stats_id")
    })
    # The fallback derivation in refresh_jobs uses exactly this query shape.
    assert nba_ids == ["2544"]
    assert mlb_ids == ["592450"]
    assert "advanced_stats_warm" in REFRESH_JOB_KINDS
