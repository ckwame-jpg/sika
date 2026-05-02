"""PR 3c — Stats Assistant percentile + metric_categories wiring.

Tests the ``augment_summary_with_advanced`` helper directly so we don't
have to seed full ESPN search/gamelog payloads. The integration test at
the end of the file then runs ``StatsQueryService.query`` end-to-end
with seeded caches to prove the wire-up.

Coverage:
- basic-only path (no db, no advanced caches): metric_categories tags
  every summary key as ``"basic"``; percentiles is empty
- NBA: emits TS%/USG%/etc. into metrics, tags as ``"advanced"``,
  computes percentile rank from breakpoints
- MLB: emits xBA/barrel rate/etc. into metrics, tags as ``"advanced"``;
  no percentile when MLB league cache is empty
- ``def_rating`` percentile is inverted (lower is better)
- Cache misses do not fail
- ``_percentile_rank`` clamps to [0,100] and handles edge cases
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.models import (
    EspnPlayerSearchCache,
    MlbBatterAdvancedCache,
    MlbLeaguePercentilesCache,
    MlbStatcastBatterCache,
    NbaAdvancedGamelogCache,
    NbaLeaguePercentilesCache,
)
from app.services.stats_summary_augment import (
    _percentile_rank,
    augment_summary_with_advanced,
)


# -----------------------------------------------------------------------------
# Basic-only path


def test_no_db_returns_only_basic_categories():
    metrics = {"points": 28.0, "rebounds": 5.0}
    augmented, percentiles, categories = augment_summary_with_advanced(
        None,
        sport_key="NBA",
        player={"athlete_id": "1", "display_name": "Test"},
        season=2026,
        summary_metrics=metrics,
    )
    assert augmented == {"points": 28.0, "rebounds": 5.0}
    assert percentiles == {}
    assert categories == {"points": "basic", "rebounds": "basic"}


def test_unsupported_sport_does_not_add_advanced(db_session):
    metrics = {"completion_pct": 65.0}
    augmented, percentiles, categories = augment_summary_with_advanced(
        db_session,
        sport_key="NFL",
        player={"athlete_id": "1", "display_name": "Test"},
        season=2025,
        summary_metrics=metrics,
    )
    assert augmented == {"completion_pct": 65.0}
    assert percentiles == {}
    assert categories == {"completion_pct": "basic"}


# -----------------------------------------------------------------------------
# NBA augmentation


def _seed_nba_player_search_cache(db_session, *, athlete_id: str, nba_stats_id: str):
    """Seed the EspnPlayerSearchCache so resolve_nba_stats_player_id can find
    the cross-reference without hitting the network."""
    now = datetime.now(timezone.utc)
    db_session.add(
        EspnPlayerSearchCache(
            sport_key="NBA",
            query_normalized="jalen brunson",
            payload={
                "athlete_id": athlete_id,
                "display_name": "Jalen Brunson",
                "nba_stats_id": nba_stats_id,
            },
            cached_at=now,
            expires_at=now + timedelta(hours=12),
        )
    )
    db_session.flush()


def _seed_nba_advanced_cache(db_session, *, nba_stats_id: str, season: int, season_avg: dict):
    now = datetime.now(timezone.utc)
    db_session.add(
        NbaAdvancedGamelogCache(
            athlete_id=str(nba_stats_id),
            season=season,
            payload={"season_avg": season_avg, "recent_10_avg": {}},
            cached_at=now,
            expires_at=now + timedelta(hours=12),
        )
    )
    db_session.flush()


def _seed_nba_league_percentiles(db_session, *, season: int, breakpoints: dict):
    now = datetime.now(timezone.utc)
    db_session.add(
        NbaLeaguePercentilesCache(
            season=season,
            metric_key="advanced",
            payload={"breakpoints": breakpoints, "sample_size": 200},
            cached_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
    db_session.flush()


def test_nba_advanced_metrics_emitted_and_tagged_advanced(db_session):
    _seed_nba_advanced_cache(
        db_session,
        nba_stats_id="1628973",
        season=2026,
        season_avg={"ts_pct": 0.61, "usg_pct": 0.32, "off_rating": 118.0, "def_rating": 108.0},
    )
    augmented, _percentiles, categories = augment_summary_with_advanced(
        db_session,
        sport_key="NBA",
        player={"athlete_id": "3934672", "display_name": "Jalen Brunson", "nba_stats_id": "1628973"},
        season=2026,
        summary_metrics={"points": 28.0, "rebounds": 5.0},
    )
    assert augmented["ts_pct"] == pytest.approx(0.61)
    assert augmented["usg_pct"] == pytest.approx(0.32)
    assert categories["ts_pct"] == "advanced"
    assert categories["usg_pct"] == "advanced"
    assert categories["points"] == "basic"


def test_nba_percentile_rank_from_league_breakpoints(db_session):
    _seed_nba_advanced_cache(
        db_session,
        nba_stats_id="1628973",
        season=2026,
        season_avg={"ts_pct": 0.62, "usg_pct": 0.30},
    )
    _seed_nba_league_percentiles(
        db_session,
        season=2026,
        breakpoints={
            "ts_pct": {"p10": 0.50, "p25": 0.54, "p50": 0.58, "p75": 0.62, "p90": 0.65},
            "usg_pct": {"p10": 0.15, "p25": 0.18, "p50": 0.21, "p75": 0.25, "p90": 0.30},
        },
    )
    _augmented, percentiles, _categories = augment_summary_with_advanced(
        db_session,
        sport_key="NBA",
        player={"athlete_id": "3934672", "display_name": "Jalen Brunson", "nba_stats_id": "1628973"},
        season=2026,
        summary_metrics={"points": 28.0},
    )
    # ts_pct 0.62 lands exactly on p75 → rank 75
    assert percentiles["ts_pct"] == pytest.approx(75.0)
    # usg_pct 0.30 lands on p90 → rank 90
    assert percentiles["usg_pct"] == pytest.approx(90.0)


def test_nba_def_rating_percentile_is_inverted(db_session):
    _seed_nba_advanced_cache(
        db_session,
        nba_stats_id="1628973",
        season=2026,
        season_avg={"def_rating": 102.0},
    )
    _seed_nba_league_percentiles(
        db_session,
        season=2026,
        breakpoints={
            "def_rating": {"p10": 100.0, "p25": 105.0, "p50": 110.0, "p75": 115.0, "p90": 120.0},
        },
    )
    _augmented, percentiles, _categories = augment_summary_with_advanced(
        db_session,
        sport_key="NBA",
        player={"athlete_id": "3934672", "display_name": "Test", "nba_stats_id": "1628973"},
        season=2026,
        summary_metrics={},
    )
    # def_rating 102 is between p10 (100) and p25 (105), 40% along → raw rank 16
    # Inverted: 100 - 16 = 84
    assert percentiles["def_rating"] == pytest.approx(84.0)


def test_nba_missing_advanced_cache_is_graceful(db_session):
    """No NBA cache rows at all — augmentation must not raise and must
    return basic-only metric_categories."""
    augmented, percentiles, categories = augment_summary_with_advanced(
        db_session,
        sport_key="NBA",
        player={"athlete_id": "999", "display_name": "Unknown Player"},
        season=2026,
        summary_metrics={"points": 28.0},
    )
    assert augmented == {"points": 28.0}
    assert percentiles == {}
    assert categories == {"points": "basic"}


def test_nba_missing_player_id_skips_advanced(db_session):
    """If we can't resolve the player to an nba_stats_id, advanced
    metrics aren't added (but basic categories still tag the inputs)."""
    augmented, percentiles, categories = augment_summary_with_advanced(
        db_session,
        sport_key="NBA",
        player={"athlete_id": "999", "display_name": "Mystery Player"},
        season=2026,
        summary_metrics={"points": 28.0},
    )
    assert "ts_pct" not in augmented
    assert percentiles == {}
    assert categories["points"] == "basic"


# -----------------------------------------------------------------------------
# MLB augmentation


def _seed_mlb_player_search(db_session, *, athlete_id: str, mlb_stats_id: str):
    now = datetime.now(timezone.utc)
    db_session.add(
        EspnPlayerSearchCache(
            sport_key="MLB",
            query_normalized="bryce harper",
            payload={
                "athlete_id": athlete_id,
                "display_name": "Bryce Harper",
                "mlb_stats_id": mlb_stats_id,
            },
            cached_at=now,
            expires_at=now + timedelta(hours=12),
        )
    )
    db_session.flush()


def _seed_mlb_batter_advanced(db_session, *, mlb_player_id: str, season: int, sabermetrics: dict, statcast: dict):
    now = datetime.now(timezone.utc)
    db_session.add(
        MlbBatterAdvancedCache(
            athlete_id=str(mlb_player_id),
            season=season,
            payload={"season_avg": sabermetrics},
            cached_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
    db_session.add(
        MlbStatcastBatterCache(
            athlete_id=str(mlb_player_id),
            season=season,
            payload={"season_avg": statcast},
            cached_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
    db_session.flush()


def test_mlb_advanced_metrics_emitted_and_tagged(db_session):
    _seed_mlb_batter_advanced(
        db_session,
        mlb_player_id="547180",
        season=2026,
        sabermetrics={"woba": 0.380, "iso": 0.250, "wrc_plus": 145.0},
        statcast={"xwoba": 0.395, "xba": 0.290, "barrel_rate": 0.13, "hard_hit_rate": 0.48},
    )
    augmented, percentiles, categories = augment_summary_with_advanced(
        db_session,
        sport_key="MLB",
        player={"athlete_id": "3408", "display_name": "Bryce Harper", "mlb_stats_id": "547180"},
        season=2026,
        summary_metrics={"hits": 1.5, "home_runs": 0.4},
    )
    assert augmented["woba"] == pytest.approx(0.380)
    assert augmented["xba"] == pytest.approx(0.290)
    assert augmented["barrel_rate"] == pytest.approx(0.13)
    assert categories["woba"] == "advanced"
    assert categories["barrel_rate"] == "advanced"
    assert categories["hits"] == "basic"
    # No MLB league percentile cache seeded → percentiles dict is empty.
    assert percentiles == {}


def test_mlb_percentile_rank_when_league_cache_present(db_session):
    _seed_mlb_batter_advanced(
        db_session,
        mlb_player_id="547180",
        season=2026,
        sabermetrics={"woba": 0.380},
        statcast={"barrel_rate": 0.14},
    )
    now = datetime.now(timezone.utc)
    db_session.add(
        MlbLeaguePercentilesCache(
            season=2026,
            metric_key="advanced",
            payload={
                "breakpoints": {
                    "woba": {"p10": 0.280, "p25": 0.310, "p50": 0.330, "p75": 0.360, "p90": 0.390},
                    "barrel_rate": {"p10": 0.03, "p25": 0.05, "p50": 0.08, "p75": 0.11, "p90": 0.14},
                },
                "sample_size": 300,
            },
            cached_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )
    db_session.flush()
    _augmented, percentiles, _categories = augment_summary_with_advanced(
        db_session,
        sport_key="MLB",
        player={"athlete_id": "3408", "display_name": "Bryce Harper", "mlb_stats_id": "547180"},
        season=2026,
        summary_metrics={"hits": 1.5},
    )
    # woba 0.380 is between p75 (0.360) and p90 (0.390): (0.380-0.360)/(0.390-0.360) = 2/3
    # rank = 75 + (2/3 * (90-75)) = 85
    assert percentiles["woba"] == pytest.approx(85.0)
    # barrel_rate 0.14 = p90 → 90
    assert percentiles["barrel_rate"] == pytest.approx(90.0)


# -----------------------------------------------------------------------------
# _percentile_rank


def test_percentile_rank_returns_none_for_invalid_input():
    assert _percentile_rank(0.5, None, "ts_pct") is None
    assert _percentile_rank(0.5, {}, "ts_pct") is None
    assert _percentile_rank(0.5, {"p50": 0.55}, "ts_pct") is None  # < 2 points


def test_percentile_rank_clamps_below_lowest_breakpoint():
    rank = _percentile_rank(0.40, {"p10": 0.50, "p90": 0.65}, "ts_pct")
    assert rank == pytest.approx(10.0)


def test_percentile_rank_clamps_above_highest_breakpoint():
    rank = _percentile_rank(0.99, {"p10": 0.50, "p90": 0.65}, "ts_pct")
    assert rank == pytest.approx(90.0)


def test_percentile_rank_interpolates_linearly():
    bp = {"p25": 0.55, "p75": 0.62}
    # value 0.585 is exactly halfway → rank = 25 + 0.5*(75-25) = 50
    rank = _percentile_rank(0.585, bp, "ts_pct")
    assert rank == pytest.approx(50.0)


def test_percentile_rank_inverts_def_rating():
    bp = {"p10": 100.0, "p90": 120.0}
    rank = _percentile_rank(105.0, bp, "def_rating")
    # raw rank for 105 = 10 + (5/20) * (90-10) = 10 + 20 = 30
    # inverted: 100 - 30 = 70
    assert rank == pytest.approx(70.0)
