"""Tests for Smarter #21 phase 2b — DB-side interval-training dataset
extraction.

These pin the contract phase 2b's CLI subcommand will consume:

- Join ``predictions`` × ``espn_player_search_cache`` ×
  ``espn_player_gamelog_cache`` on (subject_name, athlete_id, season).
- Match the game whose ``gameDate`` falls within ±36h of
  ``captured_at`` and extract the requested stat output.
- Vectorize each surviving row's ``features`` blob through the
  supplied ``FeatureSpec`` (same enrichment ``ml.dataset._prepare_frame``
  applies for the classifier).
- Surface a per-reason skip count so the operator log can explain
  why a sample of N predictions yielded M training rows.
- Gate at ``min_samples`` — return ``None`` when the extract is too
  thin to fit a regressor on.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ml.interval_dataset import (
    INTERVAL_DATASET_SKIP_REASONS,
    IntervalDatasetExtract,
    build_interval_training_rows,
    season_for_captured_at,
)
from ml_features import FeatureSpec


# A minimal but realistic ESPN NBA gamelog payload — same shape the
# live resolver consumes (see ``apps/api/tests/test_predictions.py``'s
# ``NBA_PROP_GAMELOG_PAYLOAD``). Each "evt-N" plays on day N+1 of the
# month so tests can pin a captured_at near a specific game.
_NBA_GAMELOG_PAYLOAD = {
    "names": [
        "minutes",
        "points",
        "totalRebounds",
        "assists",
        "steals",
        "blocks",
        "turnovers",
        "fieldGoalsMade-fieldGoalsAttempted",
        "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
        "freeThrowsMade-freeThrowsAttempted",
    ],
    "events": {
        f"evt-{index}": {
            "gameDate": f"2026-05-{index + 1:02d}T23:30Z",
            "opponent": {"displayName": "Boston Celtics", "abbreviation": "BOS"},
            "atVs": "vs",
            "team": {"displayName": "New York Knicks"},
            "gameResult": "W",
        }
        for index in range(5)
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {
                            "eventId": f"evt-{index}",
                            "stats": [
                                "34",                  # minutes
                                str(20 + index * 2),   # points
                                str(4 + index),        # totalRebounds
                                str(7 + index),        # assists
                                "1",                   # steals
                                "0",                   # blocks
                                "2",                   # turnovers
                                "10-18",               # FGM-FGA
                                f"{2 + (index % 3)}-7",  # 3PM-3PA
                                "5-6",                 # FTM-FTA
                            ],
                        }
                        for index in range(5)
                    ]
                }
            ]
        }
    ],
}


_MLB_GAMELOG_PAYLOAD = {
    "names": [
        "atBats",
        "runs",
        "hits",
        "doubles",
        "triples",
        "homeRuns",
        "RBIs",
        "walks",
        "hitByPitch",
        "strikeouts",
    ],
    "events": {
        f"mlb-{index}": {
            "gameDate": f"2026-05-{index + 1:02d}T19:10Z",
            "opponent": {"displayName": "Tampa Bay Rays", "abbreviation": "TBR"},
            "atVs": "vs",
            "team": {"displayName": "New York Yankees"},
        }
        for index in range(3)
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {
                            "eventId": "mlb-0",
                            # 4 AB / 2 R / 2 H / 1 2B / 0 3B / 1 HR / 3 RBI / 1 BB / 0 HBP / 0 K
                            "stats": ["4", "2", "2", "1", "0", "1", "3", "1", "0", "0"],
                        },
                        {
                            "eventId": "mlb-1",
                            "stats": ["3", "0", "1", "0", "0", "0", "0", "0", "0", "1"],
                        },
                        {
                            "eventId": "mlb-2",
                            "stats": ["5", "1", "3", "0", "1", "0", "1", "0", "0", "1"],
                        },
                    ]
                }
            ]
        }
    ],
}


_NFL_QB_GAMELOG_PAYLOAD = {
    # Mirrors ESPN's QB-shaped cached payload used by the API stats-query
    # tests: passing and rushing fields share one names vector, while a
    # missing advanced value is represented by a dash.
    "names": [
        "completions",
        "passingAttempts",
        "passingYards",
        "completionPct",
        "yardsPerPassAttempt",
        "passingTouchdowns",
        "interceptions",
        "longPassing",
        "sacks",
        "QBRating",
        "adjQBR",
        "rushingAttempts",
        "rushingYards",
        "yardsPerRushAttempt",
        "rushingTouchdowns",
        "longRushing",
    ],
    "events": {
        "nfl-qb-0": {
            "gameDate": "2026-01-04T18:00:00+00:00",
            "opponent": {"displayName": "New York Giants", "abbreviation": "NYG"},
            "atVs": "vs",
            "team": {"displayName": "Philadelphia Eagles"},
            "gameResult": "W",
        },
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {
                            "eventId": "nfl-qb-0",
                            "stats": [
                                "22", "31", "246", "71.0", "7.9", "2", "0", "41",
                                "2", "104.8", "-", "10", "61", "6.1", "1", "18",
                            ],
                        },
                    ],
                },
            ],
        },
    ],
}


_NFL_RECEIVER_GAMELOG_PAYLOAD = {
    # Receiving-first names and dash placeholders mirror a live ESPN WR
    # gamelog payload (the same shape pinned in API scaffolding tests).
    "names": [
        "receptions",
        "receivingTargets",
        "receivingYards",
        "yardsPerReception",
        "receivingTouchdowns",
        "longReception",
        "rushingAttempts",
        "rushingYards",
        "yardsPerRushAttempt",
        "longRushing",
        "rushingTouchdowns",
        "fumbles",
        "fumblesLost",
        "fumblesForced",
        "kicksBlocked",
    ],
    "events": {
        "nfl-wr-0": {
            "gameDate": "2026-01-04T18:00:00+00:00",
            "opponent": {"displayName": "Green Bay Packers", "abbreviation": "GB"},
            "atVs": "vs",
            "team": {"displayName": "Minnesota Vikings"},
            "gameResult": "W",
        },
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {
                            "eventId": "nfl-wr-0",
                            "stats": [
                                "8", "11", "101", "12.6", "1", "18", "1", "3",
                                "3.0", "3", "0", "0", "-", "-", "-",
                            ],
                        },
                    ],
                },
            ],
        },
    ],
}


_DEFAULT_FEATURE_KEYS = (
    "expected_stat_output",
    "recent_avg",
    "context_coverage",
)


def _make_feature_spec(
    keys: tuple[str, ...] = _DEFAULT_FEATURE_KEYS,
    family_keys: tuple[str, ...] = ("nba_props", "mlb_props"),
) -> FeatureSpec:
    """Mirror the artifact's feature_spec — ordered_keys + one-hot
    family encoding. ``vectorize`` lays features in this exact order so
    tests can assert against deterministic column positions."""
    return FeatureSpec(
        version="test-feature-set",
        ordered_keys=list(keys),
        default_values={key: 0.0 for key in keys},
        family_one_hot_keys=list(family_keys),
    )


def _seed_db(
    db_path: Path,
    *,
    predictions: list[dict],
    player_search: list[dict] | None = None,
    gamelogs: list[dict] | None = None,
    events: list[dict] | None = None,
) -> None:
    """Create a sqlite DB with the minimum schema interval_dataset reads.

    Schemas mirror the columns ``ml.interval_dataset`` queries — extra
    columns the production tables have but the module doesn't read are
    omitted so the fixture stays focused on what's under test.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER,
                event_id INTEGER,
                sport_key TEXT,
                market_family TEXT,
                subject_name TEXT,
                subject_team TEXT,
                stat_key TEXT,
                capture_scope TEXT,
                captured_at TEXT NOT NULL,
                features TEXT,
                suggested_price REAL,
                fair_yes_price REAL,
                edge REAL,
                confidence REAL,
                selection_score REAL,
                threshold REAL,
                prediction_outcome TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                starts_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE espn_player_search_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport_key TEXT NOT NULL,
                query_normalized TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE espn_player_gamelog_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport_key TEXT NOT NULL,
                athlete_id TEXT NOT NULL,
                season INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        for row in predictions:
            row.setdefault("market_id", None)
            row.setdefault("event_id", None)
            row.setdefault("subject_team", None)
            row.setdefault("capture_scope", "recommendation")
            row.setdefault("features", json.dumps({}))
            row.setdefault("prediction_outcome", "won")
            row.setdefault("suggested_price", 0.5)
            row.setdefault("fair_yes_price", 0.55)
            row.setdefault("edge", 0.05)
            row.setdefault("confidence", 0.6)
            row.setdefault("selection_score", 0.5)
            row.setdefault("threshold", 25.0)
            conn.execute(
                """
                INSERT INTO predictions
                    (market_id, event_id, sport_key, market_family, subject_name,
                     subject_team, stat_key, capture_scope, captured_at,
                     features, suggested_price, fair_yes_price,
                     edge, confidence, selection_score, threshold,
                     prediction_outcome)
                VALUES
                    (:market_id, :event_id, :sport_key, :market_family, :subject_name,
                     :subject_team, :stat_key, :capture_scope, :captured_at,
                     :features, :suggested_price, :fair_yes_price,
                     :edge, :confidence, :selection_score, :threshold,
                     :prediction_outcome)
                """,
                row,
            )
        for row in events or []:
            conn.execute(
                "INSERT INTO events (id, starts_at) VALUES (:id, :starts_at)",
                row,
            )
        for row in player_search or []:
            conn.execute(
                """
                INSERT INTO espn_player_search_cache
                    (sport_key, query_normalized, payload)
                VALUES (:sport_key, :query_normalized, :payload)
                """,
                row,
            )
        for row in gamelogs or []:
            conn.execute(
                """
                INSERT INTO espn_player_gamelog_cache
                    (sport_key, athlete_id, season, payload)
                VALUES (:sport_key, :athlete_id, :season, :payload)
                """,
                row,
            )
        conn.commit()
    finally:
        conn.close()


def _captured_at(year: int = 2026, month: int = 5, day: int = 1, hour: int = 17) -> str:
    """Predictions are typically captured a few hours before tip-off; the
    NBA fixture's evt-{N} plays on month-day {N+1} at 23:30Z. A 5/1 17:00
    captured_at lands ~6h before evt-0 — well within the ±36h window."""
    return datetime(year, month, day, hour, 0, tzinfo=timezone.utc).isoformat()


def _player_search_row(
    sport_key: str, subject: str, athlete_id: str, *, team_name: str | None = None,
) -> dict:
    payload = {"athlete_id": athlete_id, "display_name": subject}
    if team_name is not None:
        payload["team_name"] = team_name
    return {
        "sport_key": sport_key,
        "query_normalized": subject.lower(),
        "payload": json.dumps(payload),
    }


def _gamelog_row(
    sport_key: str, athlete_id: str, season: int, payload: dict,
) -> dict:
    return {
        "sport_key": sport_key,
        "athlete_id": athlete_id,
        "season": season,
        "payload": json.dumps(payload),
    }


# -- season_for_captured_at --------------------------------------------


def test_season_for_captured_at_nba_october_rolls_to_next_year() -> None:
    captured_at = datetime(2025, 10, 28, tzinfo=timezone.utc)
    assert season_for_captured_at("NBA", captured_at) == 2026


def test_season_for_captured_at_nba_january_keeps_current_year() -> None:
    captured_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert season_for_captured_at("NBA", captured_at) == 2026


def test_season_for_captured_at_nfl_january_uses_prior_season() -> None:
    captured_at = datetime(2026, 1, 4, tzinfo=timezone.utc)
    assert season_for_captured_at("NFL", captured_at) == 2025


def test_season_for_captured_at_nfl_august_starts_current_season() -> None:
    captured_at = datetime(2026, 8, 15, tzinfo=timezone.utc)
    assert season_for_captured_at("NFL", captured_at) == 2026


def test_season_for_captured_at_mlb_march_starts_current_year() -> None:
    captured_at = datetime(2026, 3, 30, tzinfo=timezone.utc)
    assert season_for_captured_at("MLB", captured_at) == 2026


def test_season_for_captured_at_mlb_february_belongs_to_prior_season() -> None:
    captured_at = datetime(2026, 2, 10, tzinfo=timezone.utc)
    assert season_for_captured_at("MLB", captured_at) == 2025


# -- Happy path --------------------------------------------------------


def test_parse_gamelog_entries_accepts_nfl_and_tolerates_dashes() -> None:
    from ml.interval_dataset import _parse_gamelog_entries, _parse_nfl_stat

    qb_rows = _parse_gamelog_entries("NFL", _NFL_QB_GAMELOG_PAYLOAD)
    receiver_rows = _parse_gamelog_entries("NFL", _NFL_RECEIVER_GAMELOG_PAYLOAD)

    assert len(qb_rows) == 1
    assert qb_rows[0][0] == datetime(2026, 1, 4, 18, 0, tzinfo=timezone.utc)
    assert qb_rows[0][1]["passing_yards"] == 246.0
    assert qb_rows[0][1]["rushing_yards"] == 61.0
    assert qb_rows[0][1]["qbr"] == 0.0

    assert len(receiver_rows) == 1
    assert receiver_rows[0][1]["receptions"] == 8.0
    assert receiver_rows[0][1]["receiving_targets"] == 11.0
    assert receiver_rows[0][1]["receiving_yards"] == 101.0
    assert receiver_rows[0][1]["fumbles_lost"] == 0.0
    # ESPN sometimes formats large numeric fields with separators.
    assert _parse_nfl_stat("1,024") == 1024.0


def test_nfl_stat_lookup_covers_canonical_props_and_explicit_combos() -> None:
    from ml.interval_dataset import _stat_value_from_raw_metrics

    raw_metrics = {
        "completions": 22.0,
        "passing_yards": 246.0,
        "passing_touchdowns": 2.0,
        "rushing_yards": 61.0,
        "rushing_touchdowns": 1.0,
        "receptions": 8.0,
        "receiving_yards": 101.0,
        "receiving_touchdowns": 1.0,
    }
    expected_direct = {
        "completions": 22.0,
        "passing_yards": 246.0,
        "passing_touchdowns": 2.0,
        "rushing_yards": 61.0,
        "rushing_touchdowns": 1.0,
        "receptions": 8.0,
        "receiving_yards": 101.0,
        "receiving_touchdowns": 1.0,
    }
    for stat_key, expected in expected_direct.items():
        assert _stat_value_from_raw_metrics("NFL", stat_key, raw_metrics) == expected
    assert (
        _stat_value_from_raw_metrics(
            "NFL", "rushing_yards_receiving_yards", raw_metrics,
        )
        == 162.0
    )
    assert (
        _stat_value_from_raw_metrics(
            "NFL", "passing_yards_rushing_yards", raw_metrics,
        )
        == 307.0
    )


def test_extracts_nfl_direct_and_combined_targets_end_to_end(tmp_path: Path) -> None:
    """NFL predictions reach cached gamelogs, including January games
    stored under the prior season, and produce both direct and explicitly
    enumerated combined-stat targets."""
    db_path = tmp_path / "nfl_props.db"
    captured_at = datetime(2026, 1, 4, 12, 0, tzinfo=timezone.utc).isoformat()
    _seed_db(
        db_path,
        predictions=[
            {
                "market_id": 1001,
                "sport_key": "NFL",
                "market_family": "player_prop",
                "subject_name": "Jalen Hurts",
                "subject_team": "PHI",
                "stat_key": "passing_yards_rushing_yards",
                "captured_at": captured_at,
                "features": json.dumps({"expected_stat_output": 292.5}),
            },
            {
                "market_id": 1002,
                "sport_key": "NFL",
                "market_family": "player_prop",
                "subject_name": "Justin Jefferson",
                "subject_team": "MIN",
                "stat_key": "rushing_yards_receiving_yards",
                "captured_at": captured_at,
                "features": json.dumps({"expected_stat_output": 94.5}),
            },
            {
                "market_id": 1003,
                "sport_key": "NFL",
                "market_family": "player_prop",
                "subject_name": "Justin Jefferson",
                "subject_team": "MIN",
                "stat_key": "receiving_yards",
                "captured_at": captured_at,
                "features": json.dumps({"expected_stat_output": 91.5}),
            },
        ],
        player_search=[
            _player_search_row(
                "NFL", "Jalen Hurts", "4040715", team_name="Philadelphia Eagles",
            ),
            _player_search_row(
                "NFL", "Justin Jefferson", "4262921", team_name="Minnesota Vikings",
            ),
        ],
        gamelogs=[
            _gamelog_row("NFL", "4040715", 2025, _NFL_QB_GAMELOG_PAYLOAD),
            _gamelog_row("NFL", "4262921", 2025, _NFL_RECEIVER_GAMELOG_PAYLOAD),
        ],
    )
    feature_spec = _make_feature_spec(family_keys=("nfl_props",))

    def extract_target(stat_key: str) -> IntervalDatasetExtract:
        extract = build_interval_training_rows(
            f"sqlite:///{db_path}",
            family_key="nfl_props",
            stat_key=stat_key,
            feature_spec=feature_spec,
            lookback_days=30,
            min_samples=1,
            now=datetime(2026, 1, 20, tzinfo=timezone.utc),
        )
        assert isinstance(extract, IntervalDatasetExtract)
        assert extract.sample_size == 1
        assert extract.features.shape == (1, 4)
        assert extract.features[0, -1] == 1.0
        return extract

    assert extract_target("receiving_yards").targets.tolist() == [101.0]
    assert extract_target("rushing_yards_receiving_yards").targets.tolist() == [104.0]
    assert extract_target("passing_yards_rushing_yards").targets.tolist() == [307.0]


def test_extracts_points_target_for_nba_happy_path(tmp_path: Path) -> None:
    db_path = tmp_path / "nba_points.db"
    captured_at = _captured_at(month=5, day=1, hour=17)  # evt-0 game date
    _seed_db(
        db_path,
        predictions=[
            {
                "sport_key": "NBA",
                "market_family": "player_prop",
                "subject_name": "Jalen Brunson",
                "stat_key": "points",
                "captured_at": captured_at,
                "features": json.dumps({
                    "expected_stat_output": 26.5,
                    "recent_avg": 27.1,
                    "context_coverage": 0.82,
                }),
            },
        ],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    feature_spec = _make_feature_spec()
    now = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)

    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=feature_spec,
        lookback_days=30,
        min_samples=1,
        now=now,
    )

    assert isinstance(extract, IntervalDatasetExtract)
    assert extract.sample_size == 1
    # evt-0 stats: points = 20 + 0*2 = 20
    assert extract.targets.tolist() == [20.0]
    # Feature row width = 3 ordered keys + 2 one-hot family keys.
    assert extract.features.shape == (1, 5)
    # First three columns are the feature dict values in spec order.
    np.testing.assert_allclose(extract.features[0, :3], [26.5, 27.1, 0.82])
    # Last two columns are the one-hot family encoding (nba_props=1, mlb_props=0).
    np.testing.assert_array_equal(extract.features[0, 3:], [1.0, 0.0])


def test_extracts_rebounds_target_for_nba(tmp_path: Path) -> None:
    db_path = tmp_path / "nba_rebounds.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "rebounds",
            "captured_at": _captured_at(month=5, day=2, hour=17),  # evt-1
            "features": json.dumps({"expected_stat_output": 5.0}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    feature_spec = _make_feature_spec()
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="rebounds",
        feature_spec=feature_spec,
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )

    assert extract is not None
    # evt-1 stats: totalRebounds = 4 + 1 = 5
    assert extract.targets.tolist() == [5.0]


def test_combo_stat_key_sums_components_for_nba(tmp_path: Path) -> None:
    """``points_rebounds_assists`` (the PRA combo) must sum each
    component pulled from the same game — the regressor target is the
    combo's continuous value, not three independent rows."""
    db_path = tmp_path / "nba_pra.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points_rebounds_assists",
            "captured_at": _captured_at(month=5, day=1, hour=17),  # evt-0
            "features": json.dumps({"expected_stat_output": 32.0}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points_rebounds_assists",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    # evt-0 stats: points 20 + rebounds 4 + assists 7 = 31
    assert extract.targets.tolist() == [31.0]


def test_made_threes_resolves_to_three_points_made(tmp_path: Path) -> None:
    db_path = tmp_path / "nba_threes.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "made_threes",
            "captured_at": _captured_at(month=5, day=1, hour=17),  # evt-0
            "features": json.dumps({"expected_stat_output": 2.0}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="made_threes",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    # evt-0 stats: "2-7" → 2 made
    assert extract.targets.tolist() == [2.0]


def test_extracts_total_bases_for_mlb(tmp_path: Path) -> None:
    """``total_bases`` is computed from raw_metrics (hits + 2*doubles +
    3*triples + 4*home_runs after subtracting extra-base hits out of
    hits). For mlb-0: 2 H, 1 2B, 0 3B, 1 HR → singles = 2-1-0-1 = 0,
    TB = 0 + 2*1 + 3*0 + 4*1 = 6."""
    db_path = tmp_path / "mlb_tb.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "MLB",
            "market_family": "player_prop",
            "subject_name": "Aaron Judge",
            "stat_key": "total_bases",
            "captured_at": datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc).isoformat(),
            "features": json.dumps({"expected_stat_output": 1.7}),
        }],
        player_search=[_player_search_row("MLB", "Aaron Judge", "592450")],
        gamelogs=[_gamelog_row("MLB", "592450", 2026, _MLB_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="mlb_props",
        stat_key="total_bases",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.targets.tolist() == [6.0]


# -- Window matching ---------------------------------------------------


def test_picks_nearest_game_within_window(tmp_path: Path) -> None:
    """When the gamelog has multiple games, pick the one closest to
    ``captured_at`` (within the ±36h window). Captured at 5/3 12:00
    UTC: evt-2 plays 5/3 23:30 (~11.5h away) is closest."""
    db_path = tmp_path / "nba_window.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points",
            "captured_at": datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc).isoformat(),
            "features": json.dumps({"expected_stat_output": 24.0}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    # evt-2 stats: points = 20 + 2*2 = 24
    assert extract.targets.tolist() == [24.0]


def test_skips_row_when_no_game_within_window(tmp_path: Path) -> None:
    """captured_at is 7 days after the latest game (evt-4 plays 5/5). No
    game falls within ±36h → row is skipped, ``no_matching_game``
    increments."""
    db_path = tmp_path / "nba_outside.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points",
            "captured_at": datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc).isoformat(),
            "features": json.dumps({"expected_stat_output": 25.0}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=0,  # accept empty result so we can inspect the extract
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 0
    assert extract.skipped["no_matching_game"] == 1


# -- Event-anchored disambiguation (codex round 3 P2) ----------------


_MLB_DOUBLEHEADER_PAYLOAD = {
    "names": ["atBats", "runs", "hits", "doubles", "triples", "homeRuns",
              "RBIs", "walks", "hitByPitch", "strikeouts"],
    "events": {
        # Game 1 plays in the afternoon.
        "dh-1": {
            "gameDate": "2026-05-10T19:00Z",
            "opponent": {"displayName": "Boston Red Sox", "abbreviation": "BOS"},
            "atVs": "vs",
            "team": {"displayName": "New York Yankees"},
        },
        # Game 2 plays in the evening, ~7h later, same day.
        "dh-2": {
            "gameDate": "2026-05-11T02:00Z",
            "opponent": {"displayName": "Boston Red Sox", "abbreviation": "BOS"},
            "atVs": "vs",
            "team": {"displayName": "New York Yankees"},
        },
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        # Game 1: 4 AB / 0 H / 1 K
                        {"eventId": "dh-1", "stats": ["4", "0", "0", "0", "0", "0", "0", "0", "0", "1"]},
                        # Game 2: 5 AB / 3 H / 2 HR / 4 RBI
                        {"eventId": "dh-2", "stats": ["5", "2", "3", "0", "0", "2", "4", "0", "0", "1"]},
                    ]
                }
            ]
        }
    ],
}


def test_uses_event_starts_at_to_pick_doubleheader_game_two(
    tmp_path: Path,
) -> None:
    """MLB doubleheader: a Game 2 prediction captured before Game 1
    starts. Both games are future-within-window from captured_at, so
    a captured_at-only smallest-future-delta picker would label the
    Game 2 row with Game 1's stats. Anchoring on
    ``events.starts_at`` for the prediction's ``event_id`` resolves
    correctly (codex round 3 P2)."""
    db_path = tmp_path / "doubleheader.db"
    captured_at = datetime(2026, 5, 10, 15, 0, tzinfo=timezone.utc)  # 4h before Game 1
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "MLB",
            "market_family": "player_prop",
            "subject_name": "Aaron Judge",
            "stat_key": "hits",
            "captured_at": captured_at.isoformat(),
            "event_id": 502,  # Game 2 event
            "features": json.dumps({"expected_stat_output": 1.5}),
        }],
        player_search=[_player_search_row("MLB", "Aaron Judge", "592450")],
        gamelogs=[_gamelog_row("MLB", "592450", 2026, _MLB_DOUBLEHEADER_PAYLOAD)],
        events=[{"id": 502, "starts_at": "2026-05-11T02:00:00+00:00"}],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="mlb_props",
        stat_key="hits",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1
    # Game 2 had 3 hits; Game 1 had 0. Without the event anchor we'd
    # pick Game 1 (smaller future delta).
    assert extract.targets.tolist() == [3.0]


def test_uses_event_starts_at_for_game_one_distinguishing_from_game_two(
    tmp_path: Path,
) -> None:
    """Mirror of the previous test for Game 1 — proves the anchor
    discriminates, not just biases toward later games."""
    db_path = tmp_path / "doubleheader_g1.db"
    captured_at = datetime(2026, 5, 10, 15, 0, tzinfo=timezone.utc)
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "MLB",
            "market_family": "player_prop",
            "subject_name": "Aaron Judge",
            "stat_key": "hits",
            "captured_at": captured_at.isoformat(),
            "event_id": 501,  # Game 1 event
            "features": json.dumps({"expected_stat_output": 1.5}),
        }],
        player_search=[_player_search_row("MLB", "Aaron Judge", "592450")],
        gamelogs=[_gamelog_row("MLB", "592450", 2026, _MLB_DOUBLEHEADER_PAYLOAD)],
        events=[{"id": 501, "starts_at": "2026-05-10T19:00:00+00:00"}],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="mlb_props",
        stat_key="hits",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1
    # Game 1 had 0 hits.
    assert extract.targets.tolist() == [0.0]


def test_falls_back_to_captured_at_window_when_event_id_missing(
    tmp_path: Path,
) -> None:
    """Legacy / event_id-NULL prediction rows fall back to the
    captured_at-based asymmetric window (codex round 2 P2 behavior).
    Verifies the anchor branch doesn't break the non-event path."""
    db_path = tmp_path / "event_missing.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points",
            "captured_at": _captured_at(month=5, day=1, hour=17),  # ~6.5h before evt-0
            "event_id": None,
            "features": json.dumps({"expected_stat_output": 26.5}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    # evt-0 future match via captured_at fallback.
    assert extract.targets.tolist() == [20.0]


def test_prefers_future_game_over_recent_past_game_on_back_to_back(
    tmp_path: Path,
) -> None:
    """Codex round 2 P2: on a back-to-back, captured_at sits between
    yesterday's already-played game and today's upcoming game. The
    smallest-abs-delta heuristic would pick yesterday — leaking the
    prior game's known result into the regressor target.

    Fix: prefer future games over past games. The fixture seeds a
    game on day 4 (~14h before captured_at) AND a game on day 5
    (~21h after captured_at). Smallest-abs picks day 4. Future-first
    correctly picks day 5.
    """
    db_path = tmp_path / "back_to_back.db"
    captured_at = datetime(2026, 5, 5, 1, 0, tzinfo=timezone.utc)  # ~14h after evt-3, ~22.5h before evt-4
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points",
            "captured_at": captured_at.isoformat(),
            "features": json.dumps({"expected_stat_output": 26.5}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1
    # evt-4 stats: points = 20 + 4*2 = 28 (the future game on day 5).
    # NOT evt-3 (which would be 20 + 3*2 = 26 — past game on day 4).
    assert extract.targets.tolist() == [28.0]


def test_falls_back_to_recent_past_game_when_no_future_match(
    tmp_path: Path,
) -> None:
    """When no future game falls in the +36h window, fall back to a
    past game within the -6h tolerance. Models tiny clock-skew (ESPN's
    reported gameDate marginally before sika's captured_at) without
    enabling the back-to-back misattribution."""
    db_path = tmp_path / "past_fallback.db"
    # evt-4 plays at 2026-05-05T23:30Z. Capture 2 hours after so the
    # game is just BEFORE captured_at; no future game in the window.
    captured_at = datetime(2026, 5, 6, 1, 30, tzinfo=timezone.utc)
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points",
            "captured_at": captured_at.isoformat(),
            "features": json.dumps({"expected_stat_output": 26.5}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1
    # evt-4 stats — past game within the 6h backward tolerance.
    assert extract.targets.tolist() == [28.0]


# -- Lookback window ---------------------------------------------------


def test_excludes_predictions_outside_lookback_days(tmp_path: Path) -> None:
    db_path = tmp_path / "nba_lookback.db"
    in_window = datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc)
    out_of_window = datetime(2026, 3, 1, 17, 0, tzinfo=timezone.utc)
    _seed_db(
        db_path,
        predictions=[
            {
                "sport_key": "NBA",
                "market_family": "player_prop",
                "subject_name": "Jalen Brunson",
                "stat_key": "points",
                "captured_at": in_window.isoformat(),
                "features": json.dumps({"expected_stat_output": 26.5}),
            },
            {
                "sport_key": "NBA",
                "market_family": "player_prop",
                "subject_name": "Jalen Brunson",
                "stat_key": "points",
                "captured_at": out_of_window.isoformat(),
                "features": json.dumps({"expected_stat_output": 24.0}),
            },
        ],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1


# -- Family filter -----------------------------------------------------


def test_excludes_rows_for_other_family(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed_family.db"
    captured_at = _captured_at(month=5, day=1, hour=17)
    _seed_db(
        db_path,
        predictions=[
            {
                "sport_key": "NBA",
                "market_family": "player_prop",
                "subject_name": "Jalen Brunson",
                "stat_key": "points",
                "captured_at": captured_at,
                "features": json.dumps({"expected_stat_output": 26.5}),
            },
            {
                # MLB row for the same stat_key — only included when
                # caller asks for mlb_props.
                "sport_key": "MLB",
                "market_family": "player_prop",
                "subject_name": "Aaron Judge",
                "stat_key": "hits",
                "captured_at": datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc).isoformat(),
                "features": json.dumps({"expected_stat_output": 1.2}),
            },
        ],
        player_search=[
            _player_search_row("NBA", "Jalen Brunson", "3934672"),
            _player_search_row("MLB", "Aaron Judge", "592450"),
        ],
        gamelogs=[
            _gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD),
            _gamelog_row("MLB", "592450", 2026, _MLB_GAMELOG_PAYLOAD),
        ],
    )

    nba_extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert nba_extract is not None
    assert nba_extract.sample_size == 1
    # NBA row → one-hot is nba_props=1, mlb_props=0.
    np.testing.assert_array_equal(nba_extract.features[0, 3:], [1.0, 0.0])


# -- Skip taxonomy -----------------------------------------------------


def test_skipped_count_when_subject_not_in_search_cache(tmp_path: Path) -> None:
    db_path = tmp_path / "missing_subject.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Unknown Player",
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({"expected_stat_output": 20.0}),
        }],
        player_search=[],  # no row for "Unknown Player"
        gamelogs=[],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=0,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 0
    assert extract.skipped["no_athlete_id"] == 1


def test_skipped_count_when_gamelog_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "missing_gamelog.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({"expected_stat_output": 26.5}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[],  # athlete known, no gamelog cached
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=0,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 0
    assert extract.skipped["no_gamelog"] == 1


def test_skipped_count_when_stat_value_missing_from_game(tmp_path: Path) -> None:
    """A gamelog whose matching game row is missing the stat column (or
    the made/attempted string is malformed) extracts to ``None``.
    Surfaces as ``no_stat_value`` so the operator can investigate
    upstream ESPN payload gaps without rows silently disappearing."""
    db_path = tmp_path / "missing_stat.db"
    # Same shape as _MLB_GAMELOG_PAYLOAD but with all stat columns
    # empty so _parse_number returns None for every field.
    sparse_payload = {
        "names": _MLB_GAMELOG_PAYLOAD["names"],
        "events": _MLB_GAMELOG_PAYLOAD["events"],
        "seasonTypes": [
            {
                "categories": [
                    {
                        "events": [
                            {"eventId": "mlb-0", "stats": [None] * 10},
                        ]
                    }
                ]
            }
        ],
    }
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "MLB",
            "market_family": "player_prop",
            "subject_name": "Aaron Judge",
            "stat_key": "hits",
            "captured_at": datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc).isoformat(),
            "features": json.dumps({"expected_stat_output": 1.5}),
        }],
        player_search=[_player_search_row("MLB", "Aaron Judge", "592450")],
        gamelogs=[_gamelog_row("MLB", "592450", 2026, sparse_payload)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="mlb_props",
        stat_key="hits",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=0,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 0
    assert extract.skipped["no_stat_value"] == 1


def test_skipped_count_when_features_blob_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "empty_features.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({}),  # explicitly empty
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=0,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 0
    assert extract.skipped["no_features"] == 1


def test_team_abbreviation_map_matches_apps_api_canonical_source() -> None:
    """Drift guard — apps/ml duplicates
    ``ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME`` from
    apps/api/app/clients/espn.py (apps/ml can't import apps/api).
    Read the canonical source as text, parse the supported-sport maps,
    and assert the apps/ml copy agrees. Without this, a roster update
    on the API side (new abbreviation, renamed franchise) would
    silently drift between the two surfaces."""
    import ast
    repo_root = Path(__file__).resolve().parents[3]
    canonical_path = repo_root / "apps" / "api" / "app" / "clients" / "espn.py"
    assert canonical_path.exists(), f"canonical source missing at {canonical_path}"
    source = canonical_path.read_text(encoding="utf-8")

    # Extract the literal ``ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME =
    # {...}`` block via the AST so this test isn't coupled to whitespace
    # formatting.
    module = ast.parse(source)
    canonical_map: dict[str, dict[str, str]] = {}
    for node in module.body:
        # Handle both ``X = {...}`` (Assign) and
        # ``X: dict[str, dict[str, str]] = {...}`` (AnnAssign).
        target_name: str | None = None
        value_node: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target_name = node.targets[0].id
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value_node = node.value
        if target_name == "ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME" and value_node is not None:
            canonical_map = ast.literal_eval(value_node)
            break
    assert canonical_map, "could not parse ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME"

    from ml.interval_dataset import _ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME

    assert _ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME == canonical_map


def test_skip_taxonomy_keys_match_documented_constants(tmp_path: Path) -> None:
    """Every key in the skip dict must be one of the documented
    ``INTERVAL_DATASET_SKIP_REASONS`` — protects callers (operator UI,
    CLI summary) from typos / silent additions."""
    db_path = tmp_path / "empty_dataset.db"
    _seed_db(db_path, predictions=[])
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=0,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert set(extract.skipped.keys()) == set(INTERVAL_DATASET_SKIP_REASONS)


# -- Min samples gate --------------------------------------------------


def test_returns_none_when_sample_size_below_min_samples(tmp_path: Path) -> None:
    db_path = tmp_path / "thin_dataset.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({"expected_stat_output": 26.5}),
        }],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=10,  # only 1 row available
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is None


# -- Team-hint disambiguation (codex round 1 P2 #1) -------------------


def _hinted_player_search_row(
    sport_key: str, subject: str, team_hint: str, athlete_id: str,
) -> dict:
    """Build a cache row whose ``query_normalized`` carries the
    ``"<bare>|<TEAM>"`` suffix the live resolver writes when a hint
    was supplied at lookup time."""
    return {
        "sport_key": sport_key,
        "query_normalized": f"{subject.lower()}|{team_hint.upper()}",
        "payload": json.dumps({"athlete_id": athlete_id, "display_name": subject}),
    }


def test_uses_hinted_cache_row_when_subject_team_present(tmp_path: Path) -> None:
    """A prediction with ``subject_team`` resolves to the matching
    ``<bare>|<TEAM>`` cache row — even when a bare row for a DIFFERENT
    same-named player also exists. This is the misattribution case
    codex round 1 P2 #1 flagged."""
    db_path = tmp_path / "hint_match.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "John Smith",
            "subject_team": "NYK",
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({"expected_stat_output": 18.0}),
        }],
        player_search=[
            # Bare row resolves to the WRONG John Smith (Lakers).
            _player_search_row("NBA", "John Smith", "lakers-john-smith"),
            # Hinted row resolves to the right John Smith on NYK.
            _hinted_player_search_row("NBA", "John Smith", "NYK", "knicks-john-smith"),
        ],
        gamelogs=[
            _gamelog_row("NBA", "knicks-john-smith", 2026, _NBA_GAMELOG_PAYLOAD),
        ],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1
    # evt-0 stats for the Knicks John Smith → points = 20.
    assert extract.targets.tolist() == [20.0]


def test_skips_hinted_prediction_when_only_bare_cache_row_exists(tmp_path: Path) -> None:
    """When the prediction has a ``subject_team`` hint but the cache
    only has a bare row, refuse to resolve — the bare row could be the
    wrong player (codex round 1 P2 #1). Surfaces as ``no_athlete_id``
    so the operator sees the cohort loss."""
    db_path = tmp_path / "hint_strict.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "John Smith",
            "subject_team": "NYK",
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({"expected_stat_output": 18.0}),
        }],
        player_search=[
            _player_search_row("NBA", "John Smith", "unknown-team-john-smith"),
        ],
        gamelogs=[],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=0,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 0
    assert extract.skipped["no_athlete_id"] == 1


def test_skips_unhinted_prediction_when_hinted_variant_exists(tmp_path: Path) -> None:
    """An unhinted prediction is ambiguous when the cache holds any
    hinted variant — we can't tell which team's John Smith the row
    refers to. Refuse rather than guess (codex round 1 P2 #1)."""
    db_path = tmp_path / "ambiguous_bare.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "John Smith",
            "subject_team": None,  # no hint on the prediction
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({"expected_stat_output": 18.0}),
        }],
        player_search=[
            _hinted_player_search_row("NBA", "John Smith", "NYK", "knicks-john-smith"),
        ],
        gamelogs=[
            _gamelog_row("NBA", "knicks-john-smith", 2026, _NBA_GAMELOG_PAYLOAD),
        ],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=0,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 0
    assert extract.skipped["no_athlete_id"] == 1


# -- Bare cache fallback when team_name matches the hint --------------
# Codex round 1 P2 #1 said: "only use a bare row when no hint exists OR
# when the cached team matches." The initial PR implemented the first
# clause only; the second clause was missing. This bites in production
# because sika's existing cache was warmed without team hints, so every
# bare-name row reads (e.g. "jaylen brunson") + a populated team_name
# payload ("Boston Celtics") — and every prediction reads subject_team
# = "BOS". Strict-mode rejection drops 100% of rows. Loosen to: prefer
# hinted (still safest), then fall back to bare when team_name matches.


def test_accepts_bare_cache_row_when_team_name_matches_hint(tmp_path: Path) -> None:
    """Prediction has subject_team='BOS'; cache has only a bare
    ``jaylen brown`` row whose payload's ``team_name`` is
    ``Boston Celtics``. ESPN's abbreviation map resolves BOS →
    Boston Celtics → substring match → accept."""
    db_path = tmp_path / "bare_fallback_match.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jaylen Brown",
            "subject_team": "BOS",
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({"expected_stat_output": 26.5}),
        }],
        player_search=[
            _player_search_row(
                "NBA", "Jaylen Brown", "3917376", team_name="Boston Celtics",
            ),
        ],
        gamelogs=[_gamelog_row("NBA", "3917376", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1


def test_rejects_bare_cache_row_when_team_name_does_not_match_hint(
    tmp_path: Path,
) -> None:
    """Prediction has subject_team='NYK' but the bare cache row's
    team_name is ``Boston Celtics`` — that's a DIFFERENT Jaylen Brown
    (hypothetical). Refuse to misattribute."""
    db_path = tmp_path / "bare_fallback_mismatch.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jaylen Brown",
            "subject_team": "NYK",
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({"expected_stat_output": 26.5}),
        }],
        player_search=[
            _player_search_row(
                "NBA", "Jaylen Brown", "3917376", team_name="Boston Celtics",
            ),
        ],
        gamelogs=[_gamelog_row("NBA", "3917376", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=0,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 0
    assert extract.skipped["no_athlete_id"] == 1


def test_hinted_cache_row_still_wins_over_bare_when_both_present(
    tmp_path: Path,
) -> None:
    """Hinted-preferred policy still holds when both rows exist: the
    hinted ``jaylen brown|BOS`` row's athlete_id wins, even though the
    bare row's team_name would have matched."""
    db_path = tmp_path / "hinted_preferred.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jaylen Brown",
            "subject_team": "BOS",
            "stat_key": "points",
            "captured_at": _captured_at(),
            "features": json.dumps({"expected_stat_output": 26.5}),
        }],
        player_search=[
            # Bare row → "old" athlete_id.
            _player_search_row(
                "NBA", "Jaylen Brown", "old-bare-id", team_name="Boston Celtics",
            ),
            # Hinted row → new, canonical athlete_id.
            _hinted_player_search_row(
                "NBA", "Jaylen Brown", "BOS", "canonical-hinted-id",
            ),
        ],
        gamelogs=[
            _gamelog_row("NBA", "canonical-hinted-id", 2026, _NBA_GAMELOG_PAYLOAD),
        ],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    # Hinted row's athlete_id wins → its gamelog resolves. If the bare
    # row had won we'd get no gamelog (only the hinted athlete_id is
    # cached) and the extract would be empty.
    assert extract is not None
    assert extract.sample_size == 1


def test_mlb_team_abbreviation_resolves_through_alternate_codes(
    tmp_path: Path,
) -> None:
    """MLB Kalshi ticker codes sometimes use 3-char abbreviations
    (``KCR`` for Kansas City Royals, ``AZ`` for Arizona Diamondbacks).
    The matcher must look these up in the abbreviation map, not just
    do substring contains."""
    db_path = tmp_path / "mlb_alt_codes.db"
    _seed_db(
        db_path,
        predictions=[{
            "sport_key": "MLB",
            "market_family": "player_prop",
            "subject_name": "Aaron Judge",
            "subject_team": "NYY",  # standard
            "stat_key": "hits",
            "captured_at": datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc).isoformat(),
            "features": json.dumps({"expected_stat_output": 1.5}),
        }],
        player_search=[
            _player_search_row(
                "MLB", "Aaron Judge", "592450", team_name="New York Yankees",
            ),
        ],
        gamelogs=[_gamelog_row("MLB", "592450", 2026, _MLB_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="mlb_props",
        stat_key="hits",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1


# -- Window filter (codex round 1 P2 #2) ------------------------------


def test_window_filter_works_against_sqlalchemy_space_separated_storage(
    tmp_path: Path,
) -> None:
    """SQLAlchemy stores ``DateTime(timezone=True)`` on SQLite as
    ``"YYYY-MM-DD HH:MM:SS.ffffff+00:00"`` (space separator) while a
    naive ISO bind uses ``T``. A SQL-side lexical compare would
    misorder same-day rows because space < T in ASCII.

    Seed a row in the SQLAlchemy storage format and verify the window
    filter keeps it (in window) vs drops it (out of window). Codex
    round 1 P2 #2 caught this — the fix is filtering in Python after
    ``_coerce_utc``."""
    db_path = tmp_path / "tz_format.db"
    _seed_db(db_path, predictions=[])
    # Overwrite the captured_at format directly with SQLAlchemy's
    # default SQLite serialization (space separator + microseconds).
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO predictions
                (market_id, event_id, sport_key, market_family, subject_name,
                 subject_team, stat_key, capture_scope, captured_at,
                 features, suggested_price, fair_yes_price,
                 edge, confidence, selection_score, threshold,
                 prediction_outcome)
            VALUES (?, NULL, 'NBA', 'player_prop', 'Jalen Brunson',
                    NULL, 'points', 'recommendation', ?,
                    ?, 0.5, 0.55, 0.05, 0.6, 0.5, 25.0, 'won')
            """,
            (
                111,
                # SQLAlchemy SQLite storage format — space separator,
                # microseconds, timezone offset.
                "2026-05-01 17:00:00.123456+00:00",
                json.dumps({"expected_stat_output": 26.5}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    _add_search_and_gamelog(
        db_path,
        search=_player_search_row("NBA", "Jalen Brunson", "3934672"),
        gamelog=_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD),
    )

    in_window = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert in_window is not None
    assert in_window.sample_size == 1

    out_of_window = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=1,
        min_samples=0,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert out_of_window is not None
    assert out_of_window.sample_size == 0


def _add_search_and_gamelog(
    db_path: Path, *, search: dict, gamelog: dict,
) -> None:
    """Append rows to the search + gamelog caches without re-creating
    the schema (the format-sensitivity test seeds predictions
    separately)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO espn_player_search_cache
                (sport_key, query_normalized, payload)
            VALUES (:sport_key, :query_normalized, :payload)
            """,
            search,
        )
        conn.execute(
            """
            INSERT INTO espn_player_gamelog_cache
                (sport_key, athlete_id, season, payload)
            VALUES (:sport_key, :athlete_id, :season, :payload)
            """,
            gamelog,
        )
        conn.commit()
    finally:
        conn.close()


# -- Classifier-scope mirroring (codex round 1 P2 #3) ------------------


def test_excludes_coverage_capture_scope_rows(tmp_path: Path) -> None:
    """``_prepare_frame`` filters out ``capture_scope='coverage'`` rows
    before classifier training. Phase 2b mirrors that so the regressor
    trains on the same row population the artifact's feature_spec was
    fit against (codex round 1 P2 #3)."""
    db_path = tmp_path / "coverage_filter.db"
    _seed_db(
        db_path,
        predictions=[
            {
                "sport_key": "NBA",
                "market_family": "player_prop",
                "subject_name": "Jalen Brunson",
                "stat_key": "points",
                "captured_at": _captured_at(month=5, day=1, hour=17),
                "features": json.dumps({"expected_stat_output": 26.5}),
                "capture_scope": "recommendation",
            },
            {
                "sport_key": "NBA",
                "market_family": "player_prop",
                "subject_name": "Jalen Brunson",
                "stat_key": "points",
                "captured_at": _captured_at(month=5, day=2, hour=17),
                "features": json.dumps({"expected_stat_output": 26.5}),
                "capture_scope": "coverage",  # excluded
            },
        ],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1


def test_dedupes_by_market_id_keeping_earliest_captured_at(tmp_path: Path) -> None:
    """Two predictions against the same ``market_id`` (e.g. the system
    re-captured the same prop after a snapshot refresh) collapse to
    one row — the earliest captured_at wins, matching
    ``_prepare_frame``'s ``drop_duplicates(subset=['market_id'],
    keep='first')`` with the ascending captured_at sort (codex round 1
    P2 #3)."""
    db_path = tmp_path / "dedupe_market.db"
    earliest = _captured_at(month=5, day=1, hour=12)
    later = _captured_at(month=5, day=1, hour=18)
    _seed_db(
        db_path,
        predictions=[
            {
                "sport_key": "NBA",
                "market_family": "player_prop",
                "subject_name": "Jalen Brunson",
                "stat_key": "points",
                "captured_at": later,  # inserted first, but later in time
                "features": json.dumps({"expected_stat_output": 27.5}),
                "market_id": 999,
            },
            {
                "sport_key": "NBA",
                "market_family": "player_prop",
                "subject_name": "Jalen Brunson",
                "stat_key": "points",
                "captured_at": earliest,
                "features": json.dumps({"expected_stat_output": 26.0}),
                "market_id": 999,
            },
        ],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 1
    # Earliest row's expected_stat_output was 26.0, not 27.5.
    np.testing.assert_allclose(extract.features[0, 0], 26.0)


# -- Settled outcomes only ---------------------------------------------


def test_excludes_pending_and_push_predictions(tmp_path: Path) -> None:
    """Only ``prediction_outcome in ('won', 'lost')`` provides a settled
    ground-truth stat output. Pending rows haven't been graded yet;
    push / cancelled rows weren't decisive enough to teach the model.
    """
    db_path = tmp_path / "settled_filter.db"
    base = {
        "sport_key": "NBA",
        "market_family": "player_prop",
        "subject_name": "Jalen Brunson",
        "stat_key": "points",
        "captured_at": _captured_at(),
        "features": json.dumps({"expected_stat_output": 26.5}),
    }
    _seed_db(
        db_path,
        predictions=[
            {**base, "prediction_outcome": "won"},
            {**base, "prediction_outcome": "lost"},
            {**base, "prediction_outcome": "pending"},
            {**base, "prediction_outcome": "push"},
            {**base, "prediction_outcome": "cancelled"},
        ],
        player_search=[_player_search_row("NBA", "Jalen Brunson", "3934672")],
        gamelogs=[_gamelog_row("NBA", "3934672", 2026, _NBA_GAMELOG_PAYLOAD)],
    )
    extract = build_interval_training_rows(
        f"sqlite:///{db_path}",
        family_key="nba_props",
        stat_key="points",
        feature_spec=_make_feature_spec(),
        lookback_days=30,
        min_samples=1,
        now=datetime(2026, 5, 16, tzinfo=timezone.utc),
    )
    assert extract is not None
    assert extract.sample_size == 2  # won + lost only
