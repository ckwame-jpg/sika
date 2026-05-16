"""Tests for Smarter #21 phase 2b — ``train-intervals`` CLI subcommand.

The subcommand orchestrates phase 2b end-to-end for one prop family +
stat key:

1. Resolves the artifact_dir from the manifest entry serving
   ``--family-key`` (mirrors the recalibrate CLI's resolution).
2. Loads ``FeatureSpec`` from the artifact's ``feature_spec.json``.
3. Calls ``build_interval_training_rows`` (PR 1) to extract
   ``(features, targets, skipped)``.
4. Calls ``train_prop_interval_models`` (phase 2a) to fit + persist
   the three quantile regressors under
   ``<artifact_dir>/interval_models/<stat_key>/``.
5. Prints a JSON summary covering sample size, empirical coverage,
   sidecar paths, and the per-reason skip counts.

Test surface:
- Happy path: enough samples → sidecars written, JSON summary reports
  ``applied=True`` + coverage.
- Insufficient samples → no sidecar, ``skip_reason="insufficient_samples"``.
- Dry run → no sidecar, ``skip_reason="dry_run"``, summary still
  reports the would-be paths.
- Missing family in manifest → ValueError.
- Skip counts surface for the operator (no_athlete_id, no_gamelog,
  no_matching_game).
- Fitted artifact round-trips via ``load_interval_models``.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from ml.cli import build_parser
from ml.interval_training import interval_models_paths, load_interval_models


_NBA_GAMELOG_PAYLOAD = {
    "names": [
        "minutes", "points", "totalRebounds", "assists", "steals",
        "blocks", "turnovers",
        "fieldGoalsMade-fieldGoalsAttempted",
        "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
        "freeThrowsMade-freeThrowsAttempted",
    ],
    "events": {
        # 50 games spread across 30 days — enough sample for the
        # default min_samples=50 gate.
        f"evt-{i}": {
            "gameDate": (
                datetime(2026, 4, 16, 23, 30, tzinfo=timezone.utc)
                + timedelta(hours=12 * i)
            ).isoformat().replace("+00:00", "Z"),
            "opponent": {"displayName": "Boston Celtics", "abbreviation": "BOS"},
            "atVs": "vs",
            "team": {"displayName": "New York Knicks"},
            "gameResult": "W",
        }
        for i in range(60)
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {
                            "eventId": f"evt-{i}",
                            "stats": [
                                "34", str(20 + (i % 15)), "4", "7", "1",
                                "0", "2", "10-18", "2-7", "5-6",
                            ],
                        }
                        for i in range(60)
                    ]
                }
            ]
        }
    ],
}


_FEATURE_SPEC = {
    "version": "test-feature-set",
    "ordered_keys": ["expected_stat_output", "recent_avg"],
    "default_values": {"expected_stat_output": 0.0, "recent_avg": 0.0},
    "family_one_hot_keys": ["nba_props", "mlb_props"],
}


def _seed_artifact(artifact_dir: Path) -> None:
    """Create a complete artifact dir — same triple the API loader
    requires (codex round 3 P2 tightened the check). The contents
    are placeholders except ``feature_spec.json`` which the CLI
    actually parses to vectorize prediction features."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "model.joblib").write_bytes(b"")
    (artifact_dir / "feature_spec.json").write_text(
        json.dumps(_FEATURE_SPEC), encoding="utf-8",
    )
    (artifact_dir / "training_metadata.json").write_text("{}", encoding="utf-8")


def _build_manifest(
    manifest_path: Path,
    *,
    artifact_dir: Path,
    family_key: str = "nba_props",
) -> None:
    import os
    artifact_path = Path(
        os.path.relpath(artifact_dir.resolve(), manifest_path.parent.resolve())
    ).as_posix()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": "2026-05-15",
        "serving_mode": "ml",
        "families": [
            {
                "family_key": "global_v1",
                "model_name": "global_hist_gradient_boosting_residual",
                "model_version": "2026-05-15",
                "calibration_version": "calibrated_v1",
                "feature_set_version": "public-feature-set-v2",
                "artifact_path": artifact_path,
                "serves_family_key": family_key,
                "mode": "ml",
                "metadata": {
                    "behavior": "sklearn_predict_proba",
                    "feature_mode": "residual_calibration",
                    "target_type": "yes_won",
                },
            }
        ],
        "metadata": {"source": "test"},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _seed_db(
    db_path: Path,
    *,
    predictions: list[dict],
    player_search: list[dict] | None = None,
    gamelogs: list[dict] | None = None,
) -> None:
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


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    parser = build_parser()
    args = parser.parse_args(argv)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        rc = args.func(args)
    output = captured.getvalue()
    summary = json.loads(output) if output.strip() else {}
    return rc, summary


def _seed_predictions_with_one_subject(
    db_path: Path, *, count: int, now: datetime, market_id_start: int = 1000,
) -> None:
    """Seed ``count`` distinct (market_id) predictions for Jalen Brunson
    spread across the last 30 days. Each prediction maps to a unique
    game in the seeded gamelog (game N for prediction N) so the
    captured_at-based selector picks the correct stat.
    """
    predictions = []
    # Bias predictions to be ~6h before each gamelog entry so the
    # asymmetric window picks the matching forward game cleanly.
    for i in range(count):
        game_date = datetime(2026, 4, 16, 23, 30, tzinfo=timezone.utc) + timedelta(hours=12 * i)
        captured = game_date - timedelta(hours=6)
        predictions.append({
            "market_id": market_id_start + i,
            "sport_key": "NBA",
            "market_family": "player_prop",
            "subject_name": "Jalen Brunson",
            "stat_key": "points",
            "captured_at": captured.isoformat(),
            "features": json.dumps({
                "expected_stat_output": 26.0 + i * 0.1,
                "recent_avg": 27.0 + i * 0.1,
            }),
        })
    _seed_db(
        db_path,
        predictions=predictions,
        player_search=[
            {
                "sport_key": "NBA",
                "query_normalized": "jalen brunson",
                "payload": json.dumps({
                    "athlete_id": "3934672",
                    "display_name": "Jalen Brunson",
                }),
            }
        ],
        gamelogs=[
            {
                "sport_key": "NBA",
                "athlete_id": "3934672",
                "season": 2026,
                "payload": json.dumps(_NBA_GAMELOG_PAYLOAD),
            }
        ],
    )


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin ``cli._now()`` so window calculations are deterministic."""
    fixed_now = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    from ml import cli
    monkeypatch.setattr(cli, "_now", lambda: fixed_now)
    return fixed_now


def _setup_happy_path(
    tmp_path: Path, *, sample_count: int = 55,
) -> tuple[Path, Path, Path]:
    db_path = tmp_path / "predictions.db"
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    manifest_path = tmp_path / "manifests" / "current.json"
    _seed_artifact(artifact_dir)
    _build_manifest(manifest_path, artifact_dir=artifact_dir)
    _seed_predictions_with_one_subject(
        db_path,
        count=sample_count,
        now=datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
    )
    return db_path, manifest_path, artifact_dir


# -- Happy path --------------------------------------------------------


def test_train_intervals_writes_sidecars_when_samples_sufficient(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    db_path, manifest_path, artifact_dir = _setup_happy_path(tmp_path)

    rc, summary = _run_cli([
        "train-intervals",
        "--family-key", "nba_props",
        "--stat-key", "points",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
        "--min-samples", "50",
    ])

    assert rc == 0
    assert summary["applied"] is True
    assert summary["family_key"] == "nba_props"
    assert summary["stat_key"] == "points"
    assert summary["sample_size"] >= 50
    assert "empirical_coverage" in summary
    paths = interval_models_paths(artifact_dir, "points")
    assert paths.p10.exists()
    assert paths.p50.exists()
    assert paths.p90.exists()
    assert paths.metadata.exists()
    assert summary["sidecar_paths"]["p10"] == str(paths.p10)
    assert summary["sidecar_paths"]["p50"] == str(paths.p50)
    assert summary["sidecar_paths"]["p90"] == str(paths.p90)
    assert summary["sidecar_paths"]["metadata"] == str(paths.metadata)


def test_train_intervals_sidecar_round_trips_via_load_interval_models(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """The sidecars must be loadable by phase 2c's
    ``load_interval_models`` — the serve-time loader that activates
    intervals in the scoring kernel (phase 2d). Round-trip verifies
    the contract end-to-end."""
    db_path, manifest_path, artifact_dir = _setup_happy_path(tmp_path)

    _run_cli([
        "train-intervals",
        "--family-key", "nba_props",
        "--stat-key", "points",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
        "--min-samples", "50",
    ])

    triple = load_interval_models(artifact_dir, "points")
    assert triple is not None
    p10_model, p50_model, p90_model = triple
    # Two ordered_keys + two family one-hot keys in the test feature spec.
    assert int(p10_model.n_features_in_) == 4
    # Probe at a canonical zero vector — should not raise.
    probe = np.zeros((1, 4), dtype=float)
    for model in (p10_model, p50_model, p90_model):
        model.predict(probe)


def test_train_intervals_metadata_records_family_and_stat(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    db_path, manifest_path, artifact_dir = _setup_happy_path(tmp_path)

    _run_cli([
        "train-intervals",
        "--family-key", "nba_props",
        "--stat-key", "points",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
        "--min-samples", "50",
    ])

    paths = interval_models_paths(artifact_dir, "points")
    metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
    assert metadata["family_key"] == "nba_props"
    assert metadata["stat_key"] == "points"
    assert metadata["sample_size"] >= 50
    assert metadata["window_start"] is not None
    assert metadata["window_end"] is not None


# -- Insufficient samples ---------------------------------------------


def test_train_intervals_skips_when_below_min_samples(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Below the gate → no sidecar written, summary explains why."""
    db_path, manifest_path, artifact_dir = _setup_happy_path(tmp_path, sample_count=5)

    rc, summary = _run_cli([
        "train-intervals",
        "--family-key", "nba_props",
        "--stat-key", "points",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
        "--min-samples", "50",
    ])

    assert rc == 0
    assert summary["applied"] is False
    assert summary["skip_reason"] == "insufficient_samples"
    assert summary["sample_size"] == 5
    paths = interval_models_paths(artifact_dir, "points")
    assert not paths.p10.exists()
    assert not paths.metadata.exists()


# -- Dry run -----------------------------------------------------------


def test_train_intervals_dry_run_does_not_write_sidecar(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    db_path, manifest_path, artifact_dir = _setup_happy_path(tmp_path)

    rc, summary = _run_cli([
        "train-intervals",
        "--family-key", "nba_props",
        "--stat-key", "points",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
        "--min-samples", "50",
        "--dry-run",
    ])

    assert rc == 0
    assert summary["applied"] is False
    assert summary["skip_reason"] == "dry_run"
    assert summary["sample_size"] >= 50  # extract still computed for reporting
    paths = interval_models_paths(artifact_dir, "points")
    assert not paths.p10.exists()


# -- Skip taxonomy surfaces --------------------------------------------


def test_train_intervals_summary_surfaces_skip_counts(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """A prediction whose subject isn't in the search cache surfaces as
    ``skipped["no_athlete_id"]`` in the JSON summary, even if other
    rows trained successfully. Without the surface, operators can't
    explain extract size shrinkage."""
    db_path = tmp_path / "predictions.db"
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    manifest_path = tmp_path / "manifests" / "current.json"
    _seed_artifact(artifact_dir)
    _build_manifest(manifest_path, artifact_dir=artifact_dir)
    _seed_predictions_with_one_subject(db_path, count=55, now=frozen_clock)

    # Add one more prediction with a subject that isn't in the search cache.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO predictions
                (market_id, sport_key, market_family, subject_name,
                 stat_key, capture_scope, captured_at, features,
                 suggested_price, fair_yes_price, edge, confidence,
                 selection_score, threshold, prediction_outcome)
            VALUES (?, 'NBA', 'player_prop', 'Unknown Player',
                    'points', 'recommendation', ?, ?,
                    0.5, 0.55, 0.05, 0.6, 0.5, 25.0, 'won')
            """,
            (
                9999,
                datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc).isoformat(),
                json.dumps({"expected_stat_output": 22.0, "recent_avg": 22.0}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    rc, summary = _run_cli([
        "train-intervals",
        "--family-key", "nba_props",
        "--stat-key", "points",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
        "--min-samples", "50",
    ])

    assert rc == 0
    assert summary["applied"] is True
    assert summary["skipped"]["no_athlete_id"] >= 1


# -- Manifest resolution ----------------------------------------------


def test_train_intervals_errors_when_family_missing_from_manifest(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    db_path = tmp_path / "predictions.db"
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    manifest_path = tmp_path / "manifests" / "current.json"
    _seed_artifact(artifact_dir)
    # Manifest only lists mlb_props — nba_props lookup should fail.
    _build_manifest(manifest_path, artifact_dir=artifact_dir, family_key="mlb_props")
    _seed_predictions_with_one_subject(db_path, count=55, now=frozen_clock)

    with pytest.raises(ValueError, match="nba_props"):
        _run_cli([
            "train-intervals",
            "--family-key", "nba_props",
            "--stat-key", "points",
            "--manifest-path", str(manifest_path),
            "--database-url", f"sqlite:///{db_path}",
            "--min-samples", "50",
        ])


def test_train_intervals_errors_when_artifact_dir_missing(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Manifest points at a path that doesn't exist — mirror the
    recalibrate CLI's defensive check (codex round 2 P2 on Smarter
    #20: refuse to attach a sidecar to a non-existent / incomplete
    artifact)."""
    db_path = tmp_path / "predictions.db"
    artifact_dir = tmp_path / "artifacts" / "missing"
    manifest_path = tmp_path / "manifests" / "current.json"
    # Don't call _seed_artifact — the directory won't exist.
    _build_manifest(manifest_path, artifact_dir=artifact_dir)
    _seed_predictions_with_one_subject(db_path, count=55, now=frozen_clock)

    with pytest.raises(FileNotFoundError, match="does not exist"):
        _run_cli([
            "train-intervals",
            "--family-key", "nba_props",
            "--stat-key", "points",
            "--manifest-path", str(manifest_path),
            "--database-url", f"sqlite:///{db_path}",
            "--min-samples", "50",
        ])
