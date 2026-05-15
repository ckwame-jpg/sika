"""Tests for Smarter #20 phase 2b — CLI ``recalibrate`` subcommand.

The subcommand fits a fresh isotonic recalibrator on the last
``--window-days`` of settled predictions for a given family, writes a
sidecar joblib next to the artifact, and bumps the manifest's
``calibration_version`` so the serve-time loader (phase 2c) can detect
the new sidecar.

Test surface:
- Happy path: enough samples + positive Brier improvement → sidecar
  written, manifest bumped, JSON summary reports ``applied=True``.
- Insufficient samples → no sidecar, no manifest change,
  ``skip_reason="insufficient_samples"``.
- Brier did not improve → no sidecar, no manifest change,
  ``skip_reason="no_brier_improvement"``.
- Dry run → no sidecar, no manifest change, ``skip_reason="dry_run"``.
- Family filter: rows for other families are excluded.
- Model-name filter: rows captured by other models are excluded
  (heuristic predictions don't pollute the recalibration).
- ``calibration_version`` bump is idempotent same-day and replaces a
  prior tag rather than accumulating.
- Missing family in manifest raises a clear error.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from ml.cli import build_parser, _bump_calibration_version
from ml.recalibration import (
    SIDECAR_METADATA_FILENAME,
    SIDECAR_RECALIBRATOR_FILENAME,
)


_MODEL_NAME = "global_hist_gradient_boosting_residual"
_MODEL_VERSION = "2026-05-15"
_OTHER_MODEL = "heuristic-v1"


def _seed_predictions_db(
    db_path: Path,
    *,
    rows: list[dict],
    shadow_rows: list[dict] | None = None,
) -> None:
    """Create a SQLite DB and seed both ``predictions`` and (optionally)
    ``shadow_inferences``.

    The schemas are minimum subsets of the columns the recalibrate CLI's
    queries touch. Shadow rows reference parent prediction rows by
    ``source_prediction_id``; the test helper inserts them in two passes
    so the foreign-key-style relationship resolves naturally.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fair_yes_price REAL,
                side TEXT NOT NULL,
                prediction_outcome TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                sport_key TEXT,
                market_family TEXT,
                model_name TEXT NOT NULL,
                model_version TEXT,
                scoring_diagnostics TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE shadow_inferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_prediction_id INTEGER NOT NULL,
                fair_yes_price REAL,
                captured_at TEXT NOT NULL,
                sport_key TEXT,
                market_family TEXT,
                model_name TEXT NOT NULL,
                model_version TEXT,
                model_metadata TEXT,
                inference_scope TEXT NOT NULL DEFAULT 'single'
            )
            """
        )
        # Default scoring_diagnostics / model_metadata to NULL when not
        # provided so the CLI's _resolve_raw_probability falls back to
        # fair_yes_price (matches legacy / no-recalibration rows).
        normalized_predictions = [
            {**row, "scoring_diagnostics": row.get("scoring_diagnostics")}
            for row in rows
        ]
        conn.executemany(
            """
            INSERT INTO predictions
                (fair_yes_price, side, prediction_outcome, captured_at,
                 sport_key, market_family, model_name, model_version,
                 scoring_diagnostics)
            VALUES
                (:fair_yes_price, :side, :prediction_outcome, :captured_at,
                 :sport_key, :market_family, :model_name, :model_version,
                 :scoring_diagnostics)
            """,
            normalized_predictions,
        )
        if shadow_rows:
            normalized_shadow = [
                {**row, "model_metadata": row.get("model_metadata")}
                for row in shadow_rows
            ]
            conn.executemany(
                """
                INSERT INTO shadow_inferences
                    (source_prediction_id, fair_yes_price, captured_at,
                     sport_key, market_family, model_name, model_version,
                     model_metadata, inference_scope)
                VALUES
                    (:source_prediction_id, :fair_yes_price, :captured_at,
                     :sport_key, :market_family, :model_name, :model_version,
                     :model_metadata, :inference_scope)
                """,
                normalized_shadow,
            )
        conn.commit()
    finally:
        conn.close()


def _build_manifest(
    manifest_path: Path,
    *,
    artifact_dir: Path,
    family_keys: tuple[str, ...] = ("nba_props",),
    model_name: str = _MODEL_NAME,
    model_version: str | None = _MODEL_VERSION,
    calibration_version: str = "calibrated_v1",
) -> None:
    """Write a minimal manifest under ``manifest_path``.

    ``artifact_path`` is stored as a relative path from the manifest's
    parent directory so the CLI's resolution mirrors how the training
    pipeline writes manifests in production.
    """
    artifact_path = Path(
        __import__("os").path.relpath(artifact_dir.resolve(), manifest_path.parent.resolve())
    ).as_posix()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": "2026-05-15",
        "serving_mode": "ml",
        "families": [
            {
                "family_key": "global_v1",
                "model_name": model_name,
                "model_version": model_version,
                "calibration_version": calibration_version,
                "feature_set_version": "public-feature-set-v2",
                "artifact_path": artifact_path,
                "serves_family_key": serves_key,
                "mode": "ml",
                "metadata": {
                    "behavior": "sklearn_predict_proba",
                    "feature_mode": "residual_calibration",
                    "target_type": "yes_won",
                },
            }
            for serves_key in family_keys
        ],
        "metadata": {"source": "test"},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _sidecar_paths_for(artifact_dir: Path, family_key: str) -> tuple[Path, Path]:
    """Per-family sidecar destination — mirrors ``_per_family_sidecar_dir``
    in the CLI. Tests assert against these paths so the on-disk layout
    is locked in by the test surface, not just the implementation."""
    sidecar_dir = artifact_dir / "recalibrators" / family_key
    return (
        sidecar_dir / SIDECAR_RECALIBRATOR_FILENAME,
        sidecar_dir / SIDECAR_METADATA_FILENAME,
    )


def _miscalibrated_rows(
    n: int,
    *,
    bias: float,
    now: datetime,
    sport_key: str = "NBA",
    market_family: str = "player_prop",
    model_name: str = _MODEL_NAME,
    model_version: str = _MODEL_VERSION,
    seed: int = 0,
) -> list[dict]:
    """Build settled-prediction rows whose stored ``fair_yes_price`` is
    biased relative to the empirical event rate by ``bias`` percentage
    points.

    Rows are spread across the most recent 20 days so they all fall
    inside a 30-day rolling window. Side="yes" everywhere keeps the
    target derivation simple (target = 1 iff prediction_outcome="won").
    """
    rng = np.random.default_rng(seed)
    true_rate = rng.uniform(0.2, 0.8, size=n)
    raw_probs = np.clip(true_rate + bias, 0.01, 0.99)
    outcomes = rng.binomial(1, true_rate)
    return [
        {
            "fair_yes_price": float(raw_probs[i]),
            "side": "yes",
            "prediction_outcome": "won" if outcomes[i] == 1 else "lost",
            "captured_at": (now - timedelta(days=20 * i / n)).isoformat(),
            "sport_key": sport_key,
            "market_family": market_family,
            "model_name": model_name,
            "model_version": model_version,
        }
        for i in range(n)
    ]


def _shadow_rows_against_heuristic(
    n: int,
    *,
    bias: float,
    now: datetime,
    sport_key: str = "NBA",
    market_family: str = "player_prop",
    model_name: str = _MODEL_NAME,
    model_version: str = _MODEL_VERSION,
    seed: int = 0,
    starting_prediction_id: int = 1,
) -> tuple[list[dict], list[dict]]:
    """Build a parallel pair of (heuristic prediction, model shadow row).

    Returns ``(heuristic_predictions, shadow_inferences)``. The
    heuristic-mode predictions carry the settled outcome (won/lost);
    the shadow rows carry the model's biased ``fair_yes_price`` and
    point at the matching prediction via ``source_prediction_id``.

    Models the typical pre-promotion deployment where the customer-
    facing pick is heuristic and the model runs in shadow mode.
    """
    rng = np.random.default_rng(seed)
    true_rate = rng.uniform(0.2, 0.8, size=n)
    raw_probs = np.clip(true_rate + bias, 0.01, 0.99)
    outcomes = rng.binomial(1, true_rate)
    predictions: list[dict] = []
    shadow: list[dict] = []
    for i in range(n):
        prediction_id = starting_prediction_id + i
        captured_at = (now - timedelta(days=20 * i / n)).isoformat()
        # Heuristic-mode prediction: side=yes, outcome derived from the
        # binomial draw, raw price is whatever the heuristic happened to
        # quote (irrelevant — we won't use it for recalibration since the
        # CLI filters this row out by model_name).
        predictions.append({
            "fair_yes_price": 0.5,
            "side": "yes",
            "prediction_outcome": "won" if outcomes[i] == 1 else "lost",
            "captured_at": captured_at,
            "sport_key": sport_key,
            "market_family": market_family,
            "model_name": _OTHER_MODEL,
            "model_version": "heuristic",
        })
        # Model's shadow inference for the same prediction — captures
        # what the ML model WOULD have said. fair_yes_price is the
        # biased model probability we want to recalibrate.
        shadow.append({
            "source_prediction_id": prediction_id,
            "fair_yes_price": float(raw_probs[i]),
            "captured_at": captured_at,
            "sport_key": sport_key,
            "market_family": market_family,
            "model_name": model_name,
            "model_version": model_version,
            "inference_scope": "single",
        })
    return predictions, shadow


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    """Invoke the CLI in-process and return ``(rc, parsed_summary)``."""
    import io
    import contextlib

    parser = build_parser()
    args = parser.parse_args(argv)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        rc = args.func(args)
    output = captured.getvalue()
    summary = json.loads(output) if output.strip() else {}
    return rc, summary


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pin ``cli._now()`` to a fixed UTC datetime for the whole test.

    Codex round 2 P2: without this, the suite's calibration_version
    assertions depend on the day pytest is run on, and the seeded
    rows can age out of the 30-day window. Every test that seeds
    rows anchored to a specific ``now`` must use this fixture so the
    CLI's internal clock matches the fixture's anchor.
    """
    fixed_now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    from ml import cli
    monkeypatch.setattr(cli, "_now", lambda: fixed_now)
    return fixed_now


def _seed_artifact_dir(artifact_dir: Path) -> None:
    """Create a fake but complete artifact directory.

    The CLI fails fast when any of ``model.joblib``, ``feature_spec.json``,
    or ``training_metadata.json`` is missing — same triple the API's
    ``load_sklearn_artifact`` requires (codex round 3 P2 tightened the
    check from just ``model.joblib`` to all three). Tests don't need
    real contents — placeholders satisfy the existence check.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "model.joblib").write_bytes(b"")
    (artifact_dir / "feature_spec.json").write_text("{}", encoding="utf-8")
    (artifact_dir / "training_metadata.json").write_text("{}", encoding="utf-8")


def _setup_happy_path(tmp_path: Path, *, n: int = 200, bias: float = 0.2):
    """Seed a DB + manifest + artifact dir for the happy-path tests.

    Returns ``(db_path, manifest_path, artifact_dir, now)``. The
    fixture caller is responsible for pinning the CLI clock to ``now``
    via the ``frozen_clock`` fixture.
    """
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    rows = _miscalibrated_rows(n, bias=bias, now=now, seed=2)
    _seed_predictions_db(db_path, rows=rows)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)
    return db_path, manifest_path, artifact_dir, now


# -- Happy path -------------------------------------------------------


def test_recalibrate_writes_sidecar_when_brier_improves(tmp_path: Path, frozen_clock: datetime) -> None:
    db_path, manifest_path, artifact_dir, _ = _setup_happy_path(tmp_path)

    rc, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert rc == 0
    assert summary["applied"] is True
    assert summary["family_key"] == "nba_props"
    assert summary["sample_size"] >= 100
    assert summary["brier_improvement"] > 0.0
    joblib_path, metadata_path = _sidecar_paths_for(artifact_dir, "nba_props")
    assert summary["sidecar_paths"]["joblib"] == str(joblib_path)
    assert summary["sidecar_paths"]["metadata"] == str(metadata_path)
    # Sidecar files live in the per-family subdirectory inside artifact_dir.
    assert joblib_path.exists()
    assert metadata_path.exists()
    # And NOT at the artifact_dir root — that would be the phase 2a
    # contract's bare layout, which doesn't isolate per-family fits.
    assert not (artifact_dir / SIDECAR_RECALIBRATOR_FILENAME).exists()
    # Manifest's calibration_version was bumped for the served family.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["families"][0]["calibration_version"] == "calibrated_v1+iso30d-2026-05-15"


def test_recalibrate_writes_family_key_into_sidecar_metadata(tmp_path: Path, frozen_clock: datetime) -> None:
    """Sidecar metadata records the family it was fit for as a defensive
    cross-check, even though the per-family subdirectory already isolates
    files. Phase 2c uses this to refuse a misconfigured load."""
    db_path, manifest_path, artifact_dir, _ = _setup_happy_path(tmp_path)

    _, _ = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    _, metadata_path = _sidecar_paths_for(artifact_dir, "nba_props")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["family_key"] == "nba_props"


def test_recalibrate_summary_includes_window_dates_and_metrics(tmp_path: Path, frozen_clock: datetime) -> None:
    db_path, manifest_path, _, _ = _setup_happy_path(tmp_path)

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert "window_start" in summary
    assert "window_end" in summary
    assert summary["metrics_before"]["brier"] > 0.0
    assert summary["metrics_after"]["brier"] >= 0.0
    assert summary["metrics_before"]["sample_size"] == summary["sample_size"]


# -- Skip paths --------------------------------------------------------


def test_recalibrate_skips_when_insufficient_samples(tmp_path: Path, frozen_clock: datetime) -> None:
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    # Only 20 rows — well below default min_samples=100.
    _seed_predictions_db(db_path, rows=_miscalibrated_rows(20, bias=0.2, now=now, seed=3))
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)

    rc, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert rc == 0
    assert summary["applied"] is False
    assert summary["skip_reason"] == "insufficient_samples"
    assert summary["insufficient_samples"] is True
    assert summary["sidecar_paths"] is None
    joblib_path, _ = _sidecar_paths_for(artifact_dir, "nba_props")
    assert not joblib_path.exists()
    # Manifest unchanged.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["families"][0]["calibration_version"] == "calibrated_v1"


def test_recalibrate_skips_when_brier_does_not_improve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frozen_clock: datetime,
) -> None:
    """Deterministic skip-path test.

    In-sample isotonic refit on randomly-drawn data almost always yields
    ``brier_improvement >= 0`` (Jensen). The realistic case where the
    decision tree skips on ``brier_improvement <= 0`` is when the
    rolling-window fit OVERFITS noise and would actually serve worse —
    out-of-sample territory we can't construct from synthetic in-sample
    data. Patch the recalibrator to return a known regression so the
    decision branch is unambiguously exercised.
    """
    from sklearn.isotonic import IsotonicRegression

    from ml import cli
    from ml.recalibration import CalibrationMetrics, RecalibrationResult

    db_path, manifest_path, artifact_dir, now = _setup_happy_path(tmp_path)

    fitted = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(
        np.array([0.1, 0.5, 0.9]), np.array([0.0, 0.5, 1.0]),
    )
    regression = RecalibrationResult(
        calibrator=fitted,
        metrics_before=CalibrationMetrics(brier=0.20, expected_calibration_error=0.05, sample_size=150),
        metrics_after=CalibrationMetrics(brier=0.25, expected_calibration_error=0.10, sample_size=150),
        window_start=now - timedelta(days=30),
        window_end=now,
        sample_size=150,
        insufficient_samples=False,
    )
    monkeypatch.setattr(cli, "recalibrate_with_rolling_window", lambda *a, **k: regression)

    rc, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert rc == 0
    assert summary["brier_improvement"] < 0
    assert summary["applied"] is False
    assert summary["skip_reason"] == "no_brier_improvement"
    joblib_path, _ = _sidecar_paths_for(artifact_dir, "nba_props")
    assert not joblib_path.exists()
    # Manifest unchanged.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["families"][0]["calibration_version"] == "calibrated_v1"


def test_recalibrate_dry_run_does_not_write(tmp_path: Path, frozen_clock: datetime) -> None:
    db_path, manifest_path, artifact_dir, _ = _setup_happy_path(tmp_path)

    rc, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
        "--dry-run",
    ])

    assert rc == 0
    assert summary["applied"] is False
    assert summary["dry_run"] is True
    assert summary["skip_reason"] == "dry_run"
    # Even though the recalibrator was successfully fit, nothing was
    # persisted — operators use this to preview improvement.
    assert summary["brier_improvement"] > 0.0
    joblib_path, _ = _sidecar_paths_for(artifact_dir, "nba_props")
    assert not joblib_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["families"][0]["calibration_version"] == "calibrated_v1"


# -- Family + model_name filters --------------------------------------


def test_recalibrate_filters_by_family_key(tmp_path: Path, frozen_clock: datetime) -> None:
    """Rows whose family doesn't match must NOT be counted toward the
    sample size or fed into the recalibrator."""
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    nba_rows = _miscalibrated_rows(
        150, bias=0.2, now=now, sport_key="NBA", market_family="player_prop", seed=4,
    )
    mlb_rows = _miscalibrated_rows(
        150, bias=0.2, now=now, sport_key="MLB", market_family="player_prop", seed=5,
    )
    _seed_predictions_db(db_path, rows=nba_rows + mlb_rows)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir, family_keys=("nba_props",))

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert summary["sample_size"] == 150  # nba only, mlb excluded


def test_recalibrate_reads_shadow_inferences_when_model_in_shadow_mode(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Codex P1 (review round 1): the typical pre-promotion deployment
    has heuristic predictions in ``predictions`` and the model's outputs
    in ``shadow_inferences``. The CLI must JOIN through to recover the
    settled outcome from the parent prediction so it can recalibrate
    BEFORE the model promotes (otherwise it's useless during the period
    operators most need it).
    """
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    heuristic_predictions, shadow_rows = _shadow_rows_against_heuristic(
        150, bias=0.2, now=now, seed=42,
    )
    _seed_predictions_db(db_path, rows=heuristic_predictions, shadow_rows=shadow_rows)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)

    rc, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert rc == 0
    # All 150 rows came from shadow_inferences; predictions table only
    # has heuristic rows that the model_name filter excludes.
    assert summary["sample_size"] == 150
    assert summary["applied"] is True
    joblib_path, _ = _sidecar_paths_for(artifact_dir, "nba_props")
    assert joblib_path.exists()


def test_recalibrate_dedups_overlapping_predictions_and_shadow_rows(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """A market that recorded BOTH a live model prediction AND a shadow
    inference (e.g. transient overlap during promotion) must not be
    double-counted toward the sample size."""
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    # 100 ml-mode predictions live in the predictions table.
    ml_rows = _miscalibrated_rows(100, bias=0.2, now=now, seed=22)
    # Re-record the same 100 rows as shadow inferences pointing at the
    # same parent predictions — same captured_at, same fair_yes_price,
    # same side. The CLI should drop these duplicates.
    shadow_dupes = [
        {
            "source_prediction_id": i + 1,
            "fair_yes_price": ml_rows[i]["fair_yes_price"],
            "captured_at": ml_rows[i]["captured_at"],
            "sport_key": ml_rows[i]["sport_key"],
            "market_family": ml_rows[i]["market_family"],
            "model_name": _MODEL_NAME,
            "model_version": _MODEL_VERSION,
            "inference_scope": "single",
        }
        for i in range(100)
    ]
    _seed_predictions_db(db_path, rows=ml_rows, shadow_rows=shadow_dupes)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    # 100 unique markets, regardless of the overlap.
    assert summary["sample_size"] == 100


def test_recalibrate_filters_by_model_version_from_manifest(tmp_path: Path, frozen_clock: datetime) -> None:
    """Codex P2 (review round 1): when a retrain creates a new artifact
    with the same ``model_name`` but a different ``model_version``, the
    CLI must NOT pull rows from prior artifacts into the rolling window.
    """
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    current_rows = _miscalibrated_rows(
        150, bias=0.2, now=now, model_version=_MODEL_VERSION, seed=33,
    )
    older_rows = _miscalibrated_rows(
        150, bias=0.2, now=now, model_version="2026-04-01", seed=34,
    )
    _seed_predictions_db(db_path, rows=current_rows + older_rows)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir, model_version=_MODEL_VERSION)

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert summary["sample_size"] == 150
    assert summary["model_version"] == _MODEL_VERSION


def test_recalibrate_legacy_manifest_without_model_version_accepts_any(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Manifests written before model_version was added (None field)
    relax the filter to "any version" — otherwise legacy artifacts
    couldn't be recalibrated at all.
    """
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    rows = _miscalibrated_rows(150, bias=0.2, now=now, model_version="anything", seed=44)
    _seed_predictions_db(db_path, rows=rows)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir, model_version=None)

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert summary["sample_size"] == 150
    assert summary["model_version"] is None


def test_recalibrate_keeps_per_family_sidecars_isolated(tmp_path: Path, frozen_clock: datetime) -> None:
    """Codex P1 (review round 1): two families served from the SAME
    artifact_dir must each get their own sidecar — the second
    invocation must NOT overwrite the first family's fit.
    """
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    # Different fair_yes_price distributions per family so we can tell
    # the persisted recalibrators apart.
    nba = _miscalibrated_rows(
        150, bias=0.2, now=now, sport_key="NBA", market_family="player_prop", seed=55,
    )
    mlb = _miscalibrated_rows(
        150, bias=0.3, now=now, sport_key="MLB", market_family="player_prop", seed=66,
    )
    _seed_predictions_db(db_path, rows=nba + mlb)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(
        manifest_path,
        artifact_dir=artifact_dir,
        family_keys=("nba_props", "mlb_props"),
    )

    _, _ = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])
    _, _ = _run_cli([
        "recalibrate",
        "--family-key", "mlb_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    nba_joblib, nba_metadata = _sidecar_paths_for(artifact_dir, "nba_props")
    mlb_joblib, mlb_metadata = _sidecar_paths_for(artifact_dir, "mlb_props")
    assert nba_joblib.exists() and mlb_joblib.exists()
    # Each metadata file was written by its own invocation.
    assert json.loads(nba_metadata.read_text(encoding="utf-8"))["family_key"] == "nba_props"
    assert json.loads(mlb_metadata.read_text(encoding="utf-8"))["family_key"] == "mlb_props"
    # Both families' manifest entries were bumped.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    versions_by_family = {
        entry["serves_family_key"]: entry["calibration_version"]
        for entry in manifest["families"]
    }
    assert versions_by_family["nba_props"] == "calibrated_v1+iso30d-2026-05-15"
    assert versions_by_family["mlb_props"] == "calibrated_v1+iso30d-2026-05-15"


def test_recalibrate_filters_by_model_name_from_manifest(tmp_path: Path, frozen_clock: datetime) -> None:
    """Heuristic predictions are NOT used to recalibrate the ML model.

    A row written by ``heuristic-v1`` shouldn't be fed into a
    recalibrator that exists to fix drift in
    ``global_hist_gradient_boosting_residual``'s output.
    """
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    ml_rows = _miscalibrated_rows(150, bias=0.2, now=now, model_name=_MODEL_NAME, seed=6)
    heuristic_rows = _miscalibrated_rows(150, bias=0.2, now=now, model_name=_OTHER_MODEL, seed=7)
    _seed_predictions_db(db_path, rows=ml_rows + heuristic_rows)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir, model_name=_MODEL_NAME)

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert summary["sample_size"] == 150
    assert summary["model_name"] == _MODEL_NAME


def test_recalibrate_excludes_pending_and_pushed_outcomes(tmp_path: Path, frozen_clock: datetime) -> None:
    """Only ``won`` and ``lost`` rows count — ``pending``, ``push``,
    ``cancelled`` must not feed the recalibrator."""
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    settled = _miscalibrated_rows(120, bias=0.2, now=now, seed=8)
    junk_rows: list[dict] = []
    for outcome in ("pending", "push", "cancelled"):
        for i in range(30):
            junk_rows.append({
                "fair_yes_price": 0.5,
                "side": "yes",
                "prediction_outcome": outcome,
                "captured_at": (now - timedelta(days=1)).isoformat(),
                "sport_key": "NBA",
                "market_family": "player_prop",
                "model_name": _MODEL_NAME,
                "model_version": _MODEL_VERSION,
            })
    _seed_predictions_db(db_path, rows=settled + junk_rows)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert summary["sample_size"] == 120


def test_recalibrate_excludes_rows_outside_window(tmp_path: Path, frozen_clock: datetime) -> None:
    """Rows captured >30 days ago must not enter the rolling window."""
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    db_path = tmp_path / "test.db"
    in_window = _miscalibrated_rows(120, bias=0.2, now=now, seed=9)
    out_of_window: list[dict] = []
    for i in range(50):
        out_of_window.append({
            "fair_yes_price": 0.5,
            "side": "yes",
            "prediction_outcome": "won",
            "captured_at": (now - timedelta(days=100 + i)).isoformat(),
            "sport_key": "NBA",
            "market_family": "player_prop",
            "model_name": _MODEL_NAME,
            "model_version": _MODEL_VERSION,
        })
    _seed_predictions_db(db_path, rows=in_window + out_of_window)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert summary["sample_size"] == 120


# -- calibration_version bump semantics --------------------------------


def test_bump_calibration_version_appends_iso30d_tag() -> None:
    today = datetime(2026, 5, 15, tzinfo=timezone.utc)
    bumped = _bump_calibration_version("calibrated_v1", today=today)
    assert bumped == "calibrated_v1+iso30d-2026-05-15"


def test_bump_calibration_version_replaces_existing_iso30d_tag() -> None:
    """Re-running on a different day must not accumulate tags."""
    today = datetime(2026, 5, 20, tzinfo=timezone.utc)
    bumped = _bump_calibration_version(
        "calibrated_v1+iso30d-2026-05-15", today=today,
    )
    assert bumped == "calibrated_v1+iso30d-2026-05-20"


def test_bump_calibration_version_idempotent_same_day() -> None:
    today = datetime(2026, 5, 15, tzinfo=timezone.utc)
    once = _bump_calibration_version("calibrated_v1", today=today)
    twice = _bump_calibration_version(once, today=today)
    assert once == twice


# -- Error paths -------------------------------------------------------


def test_recalibrate_raises_when_artifact_dir_missing(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Codex round 2 P2: don't create a sidecar for a model artifact
    that doesn't exist on disk. Refuse at resolve time."""
    now = frozen_clock
    db_path = tmp_path / "test.db"
    _seed_predictions_db(db_path, rows=_miscalibrated_rows(150, bias=0.2, now=now, seed=70))
    nonexistent_artifact_dir = tmp_path / "artifacts" / "global_v1_GONE"
    # Intentionally do NOT create the artifact dir.
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=nonexistent_artifact_dir)

    with pytest.raises(FileNotFoundError, match="does not exist"):
        _run_cli([
            "recalibrate",
            "--family-key", "nba_props",
            "--manifest-path", str(manifest_path),
            "--database-url", f"sqlite:///{db_path}",
        ])


def test_recalibrate_raises_when_artifact_dir_missing_model(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Codex round 2 P2: artifact_dir exists but lacks model.joblib —
    the artifact is incomplete (or the manifest points at the wrong
    directory). Refuse rather than attach a sidecar to nothing."""
    now = frozen_clock
    db_path = tmp_path / "test.db"
    _seed_predictions_db(db_path, rows=_miscalibrated_rows(150, bias=0.2, now=now, seed=71))
    artifact_dir = tmp_path / "artifacts" / "global_v1_INCOMPLETE"
    artifact_dir.mkdir(parents=True, exist_ok=True)  # exists but empty
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)

    with pytest.raises(FileNotFoundError, match="missing required file"):
        _run_cli([
            "recalibrate",
            "--family-key", "nba_props",
            "--manifest-path", str(manifest_path),
            "--database-url", f"sqlite:///{db_path}",
        ])


def test_recalibrate_raises_when_artifact_dir_missing_feature_spec(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Codex round 3 P2: ``model.joblib`` alone isn't enough — the
    runtime requires ``feature_spec.json`` and ``training_metadata.json``
    too. Recalibrating an artifact that the runtime can't load would
    waste an iso30d tag on a manifest entry that fails at serve time.
    """
    now = frozen_clock
    db_path = tmp_path / "test.db"
    _seed_predictions_db(db_path, rows=_miscalibrated_rows(150, bias=0.2, now=now, seed=72))
    artifact_dir = tmp_path / "artifacts" / "global_v1_PARTIAL"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "model.joblib").write_bytes(b"")
    # Intentionally do NOT write feature_spec.json or training_metadata.json.
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)

    with pytest.raises(FileNotFoundError, match="feature_spec.json"):
        _run_cli([
            "recalibrate",
            "--family-key", "nba_props",
            "--manifest-path", str(manifest_path),
            "--database-url", f"sqlite:///{db_path}",
        ])


def test_recalibrate_resolves_serves_family_key_from_metadata_fallback(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Codex round 3 P2: registry.py reads ``serves_family_key`` from
    ``entry["metadata"]["serves_family_key"]`` as a fallback. Manifests
    written that way are valid for the API runtime, so the CLI must
    accept them too."""
    now = frozen_clock
    db_path = tmp_path / "test.db"
    _seed_predictions_db(db_path, rows=_miscalibrated_rows(150, bias=0.2, now=now, seed=73))
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    artifact_path = Path(
        __import__("os").path.relpath(artifact_dir.resolve(), manifest_path.parent.resolve())
    ).as_posix()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    # Construct a manifest WHERE serves_family_key lives in metadata,
    # not at the top level — same shape registry.py accepts.
    manifest_path.write_text(json.dumps({
        "version": "2026-05-15",
        "serving_mode": "ml",
        "families": [{
            "family_key": "global_v1",
            "model_name": _MODEL_NAME,
            "model_version": _MODEL_VERSION,
            "calibration_version": "calibrated_v1",
            "feature_set_version": "public-feature-set-v2",
            "artifact_path": artifact_path,
            "mode": "ml",
            "metadata": {"serves_family_key": "nba_props"},
        }],
        "metadata": {"source": "test"},
    }, indent=2), encoding="utf-8")

    rc, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert rc == 0
    assert summary["applied"] is True


def test_recalibrate_resolves_family_key_from_top_level_when_serves_missing(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Codex round 3 P2: runtime.py:_manifest_family_map keys an entry
    by ``family_key`` when ``serves_family_key`` is None. The CLI
    must use the same fallback so that a manifest valid for the
    runtime resolves cleanly here.
    """
    now = frozen_clock
    db_path = tmp_path / "test.db"
    _seed_predictions_db(db_path, rows=_miscalibrated_rows(150, bias=0.2, now=now, seed=74))
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    artifact_path = Path(
        __import__("os").path.relpath(artifact_dir.resolve(), manifest_path.parent.resolve())
    ).as_posix()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    # No serves_family_key anywhere — runtime would key the entry by
    # family_key directly. The CLI's --family-key arg should match
    # against family_key in this case.
    manifest_path.write_text(json.dumps({
        "version": "2026-05-15",
        "serving_mode": "ml",
        "families": [{
            "family_key": "nba_props",
            "model_name": _MODEL_NAME,
            "model_version": _MODEL_VERSION,
            "calibration_version": "calibrated_v1",
            "feature_set_version": "public-feature-set-v2",
            "artifact_path": artifact_path,
            "mode": "ml",
            "metadata": {},
        }],
        "metadata": {"source": "test"},
    }, indent=2), encoding="utf-8")

    rc, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert rc == 0
    assert summary["applied"] is True


def test_recalibrate_prefers_raw_probability_from_scoring_diagnostics(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Phase 2c persists the model's RAW probability into
    ``scoring_diagnostics.raw_probability`` whenever a recalibrator
    fired at serve time. Subsequent CLI runs MUST fit on the raw
    distribution, not on the post-recalibration ``fair_yes_price``
    (which is a different input scale). Codex round 2 P1 on phase 2c.
    """
    now = frozen_clock
    db_path = tmp_path / "test.db"
    rows = []
    # 150 rows where fair_yes_price (post-recalibration) differs from
    # the raw value preserved in scoring_diagnostics. The CLI should
    # train on the raw values, so summary's metrics_before should
    # reflect the raw distribution (constant ~0.7), not fair_yes_price.
    for i in range(150):
        rows.append({
            "fair_yes_price": 0.55,  # post-recalibration (irrelevant for refit)
            "side": "yes",
            "prediction_outcome": "won" if i % 2 == 0 else "lost",
            "captured_at": (now - timedelta(days=20 * i / 150)).isoformat(),
            "sport_key": "NBA",
            "market_family": "player_prop",
            "model_name": _MODEL_NAME,
            "model_version": _MODEL_VERSION,
            "scoring_diagnostics": json.dumps({
                "recalibration_applied": True,
                "raw_probability": 0.70,
            }),
        })
    _seed_predictions_db(db_path, rows=rows)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert summary["sample_size"] == 150
    # The recalibrator was fit on raw=0.70 inputs against 50/50 outcomes.
    # Brier_before = mean((0.70 - outcome)^2). For 75 wins / 75 losses:
    # (75 * (0.70-1)^2 + 75 * (0.70-0)^2) / 150 = (75 * 0.09 + 75 * 0.49) / 150 = 0.29
    # Brier_before reflects the raw distribution, not fair_yes_price=0.55.
    assert 0.27 < summary["metrics_before"]["brier"] < 0.31


def test_recalibrate_does_not_dedupe_distinct_predictions_with_same_probability(
    tmp_path: Path, frozen_clock: datetime,
) -> None:
    """Codex round 2 P2: distinct settled predictions whose stored
    fair_yes_price happens to round to the same 4-decimal value (a
    real risk for batch captures) must NOT be collapsed by the dedup.

    Build 200 rows that all share fair_yes_price=0.5500 and side=yes
    but have distinct prediction.ids and distinct captured_at values
    spread across the window. With the (captured_at, side, raw_prob)
    key we used to consider, distinct timestamps already differentiated
    them; with a (side, raw_prob) key they would all collapse to one.
    The new id-based dedup keeps every distinct prediction.
    """
    now = frozen_clock
    db_path = tmp_path / "test.db"
    rows = []
    # 200 rows, same captured_at, same side, same raw_prob — only the
    # auto-incrementing id distinguishes them. This is the worst-case
    # scenario the rounded-probability key would silently collapse.
    same_moment = (now - timedelta(days=1)).isoformat()
    for _ in range(200):
        rows.append({
            "fair_yes_price": 0.55,
            "side": "yes",
            "prediction_outcome": "won",
            "captured_at": same_moment,
            "sport_key": "NBA",
            "market_family": "player_prop",
            "model_name": _MODEL_NAME,
            "model_version": _MODEL_VERSION,
        })
    _seed_predictions_db(db_path, rows=rows)
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260515"
    _seed_artifact_dir(artifact_dir)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, artifact_dir=artifact_dir)

    _, summary = _run_cli([
        "recalibrate",
        "--family-key", "nba_props",
        "--manifest-path", str(manifest_path),
        "--database-url", f"sqlite:///{db_path}",
    ])

    # All 200 distinct prediction ids retained.
    assert summary["sample_size"] == 200


def test_recalibrate_raises_when_family_not_in_manifest(tmp_path: Path, frozen_clock: datetime) -> None:
    db_path, manifest_path, _, _ = _setup_happy_path(tmp_path)

    with pytest.raises(ValueError, match="not found in manifest"):
        _run_cli([
            "recalibrate",
            "--family-key", "nba_singles",  # not in manifest
            "--manifest-path", str(manifest_path),
            "--database-url", f"sqlite:///{db_path}",
        ])
