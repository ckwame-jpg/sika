"""PR 3d — ML v2 training path additions.

Covers:
  - median imputation: ``FeatureSpec.default_values`` reflects the median
    of present values per key (not 0.0)
  - sample weights: rows with an advanced completeness marker get weighted
    `advanced_sample_weight`x; the metadata records that
  - completeness counts: per-family + total
  - advanced-only mode: triggered when a family has ≥ threshold complete
    rows; filters dataset and disables median imputation
  - promotion gate: serving_mode in the manifest reflects whether the
    candidate Brier beat the supplied baseline
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from ml.dataset import settled_predictions_from_records
from ml.training import (
    ADVANCED_COMPLETENESS_MARKERS,
    _advanced_completeness_counts,
    _advanced_completeness_mask,
    _build_sample_weights,
    _compute_feature_medians,
    _row_is_advanced_complete,
    build_feature_spec,
    train_and_package,
)


def _records(
    total: int = 240,
    *,
    advanced_complete_share: float = 0.0,
    advanced_only_for_family: str | None = None,
    advanced_only_count: int = 0,
):
    """Generate a synthetic settled-predictions dataset.

    ``advanced_complete_share`` — fraction of rows that get
    ``advanced_data_complete=1.0`` and a real ``ts_pct`` value.

    ``advanced_only_for_family`` + ``advanced_only_count`` — overrides the
    share to seed enough advanced-complete rows under a specific family
    for the threshold-trigger test.
    """
    base = datetime(2026, 4, 17, 18, 0, tzinfo=timezone.utc)
    rows = []
    advanced_complete_target = int(total * advanced_complete_share)
    family_complete_count = 0
    for index in range(total):
        sport = "MLB" if index % 2 == 0 else "NBA"
        family = "mlb_props" if sport == "MLB" else "nba_props"
        recent_average = float(index % 12) + (2.0 if sport == "MLB" else 0.0)
        threshold = float(index % 10) + 4.5
        won = recent_average + (1.0 if family == "mlb_props" else 0.0) > threshold
        is_advanced = False
        if advanced_only_for_family and family == advanced_only_for_family:
            if family_complete_count < advanced_only_count:
                is_advanced = True
                family_complete_count += 1
        elif index < advanced_complete_target:
            is_advanced = True

        features: dict[str, float | bool] = {
            "family_key": family,
            "recent_average": recent_average,
            "threshold": threshold,
            "yes_probability": 0.62 if won else 0.41,
            "has_team_context": True,
            "latest_log_days_ago": index % 4,
        }
        if is_advanced:
            features["advanced_data_complete"] = 1.0
            features["ts_pct"] = 0.55 + ((index % 5) * 0.01)

        rows.append(
            {
                "id": index + 1,
                "market_id": index + 1,
                "event_id": (index // 6) + 1,
                "ticker": f"TEST-{index}",
                "sport_key": sport,
                "event_name": f"Event {index // 6}",
                "market_family": "player_prop",
                "market_kind": "player_prop",
                "stat_key": "hits" if sport == "MLB" else "points",
                "threshold": threshold,
                "subject_name": f"Player {index % 24}",
                "subject_team": f"Team {index % 8}",
                "capture_scope": "recommendation",
                "side": "yes",
                "suggested_price": 0.44 + ((index % 5) * 0.02),
                "fair_yes_price": 0.55 if won else 0.42,
                "edge": 0.08 if won else -0.05,
                "confidence": 0.62,
                "selection_score": 0.12,
                "features": features,
                "scoring_diagnostics": {},
                "market_status_at_capture": "active",
                "prediction_outcome": "won" if won else "lost",
                "settled_at": (base + timedelta(hours=index)).isoformat(),
                "realized_pnl": 0.56 if won else -0.44,
                "captured_at": (base + timedelta(minutes=index)).isoformat(),
            }
        )
    return rows


# -----------------------------------------------------------------------------
# Helpers


def test_row_is_advanced_complete_detects_marker():
    assert _row_is_advanced_complete({"advanced_data_complete": 1.0}) is True
    assert _row_is_advanced_complete({"mlb_batter_data_complete": 1.0}) is True
    assert _row_is_advanced_complete({"advanced_data_complete": 0.0}) is False
    assert _row_is_advanced_complete({}) is False


def test_advanced_completeness_markers_cover_known_emitters():
    # Sanity-check the marker list matches the constants the API emitters
    # actually write — when a new emitter is added, this test will need
    # to grow. (No new emitters were added in PR 3d, so all six existing
    # markers should be present.)
    expected = {
        "advanced_data_complete",
        "mlb_batter_data_complete",
        "pitcher_data_complete",
        "opponent_team_data_complete",
        "park_data_complete",
        "weather_data_complete",
    }
    assert set(ADVANCED_COMPLETENESS_MARKERS) == expected


def test_advanced_completeness_counts_per_family():
    frame = settled_predictions_from_records(
        _records(total=120, advanced_complete_share=0.5)
    )
    counts = _advanced_completeness_counts(frame)
    assert counts["__total__"] == frame["features"].apply(_row_is_advanced_complete).sum()
    assert counts["nba_props"] >= 0
    assert counts["mlb_props"] >= 0
    assert counts["__total__"] == counts["nba_props"] + counts["mlb_props"]


def test_advanced_completeness_mask_aligns_with_frame():
    frame = settled_predictions_from_records(
        _records(total=60, advanced_complete_share=0.4)
    )
    mask = _advanced_completeness_mask(frame)
    assert len(mask) == len(frame)
    # Should match the per-row check.
    expected = np.asarray([_row_is_advanced_complete(f) for f in frame["features"]], dtype=bool)
    np.testing.assert_array_equal(mask, expected)


# -----------------------------------------------------------------------------
# Median imputation


def test_compute_feature_medians_uses_present_values_only():
    frame = pd.DataFrame(
        {
            "features": [
                {"a": 1.0, "b": 10.0},
                {"a": 3.0},  # b missing
                {"a": 5.0, "b": 30.0},
            ],
        }
    )
    medians = _compute_feature_medians(frame, ["a", "b", "absent"])
    assert medians["a"] == pytest.approx(3.0)
    assert medians["b"] == pytest.approx(20.0)
    assert medians["absent"] == 0.0  # no values → fall back to 0.0


def test_build_feature_spec_uses_medians_when_imputation_enabled():
    frame = settled_predictions_from_records(
        _records(total=60, advanced_complete_share=0.5)
    )
    spec = build_feature_spec(frame, version="public-feature-set-v2", use_median_imputation=True)
    # ``ts_pct`` was set on roughly half the rows. The median of those rows
    # is ~0.55, NOT 0.0.
    assert "ts_pct" in spec.default_values
    assert spec.default_values["ts_pct"] != 0.0
    assert 0.5 <= spec.default_values["ts_pct"] <= 0.6


def test_build_feature_spec_keeps_zero_default_when_imputation_disabled():
    frame = settled_predictions_from_records(
        _records(total=60, advanced_complete_share=0.5)
    )
    spec = build_feature_spec(frame, version="public-feature-set-v2", use_median_imputation=False)
    # Pre-PR3d behaviour: every default is 0.0.
    assert spec.default_values["ts_pct"] == 0.0


# -----------------------------------------------------------------------------
# Sample weights


def test_build_sample_weights_assigns_advanced_weight_to_complete_rows():
    frame = settled_predictions_from_records(
        _records(total=20, advanced_complete_share=0.5)
    )
    weights = _build_sample_weights(frame, advanced_weight=3.0)
    assert weights.shape == (len(frame),)
    mask = _advanced_completeness_mask(frame)
    np.testing.assert_array_equal(weights[mask], 3.0)
    np.testing.assert_array_equal(weights[~mask], 1.0)


def test_build_sample_weights_returns_uniform_when_weight_is_one():
    frame = settled_predictions_from_records(
        _records(total=20, advanced_complete_share=0.5)
    )
    weights = _build_sample_weights(frame, advanced_weight=1.0)
    np.testing.assert_array_equal(weights, np.ones(len(frame)))


# -----------------------------------------------------------------------------
# train_and_package — integration


def test_training_with_advanced_data_records_completeness_metadata(tmp_path):
    frame = settled_predictions_from_records(
        _records(total=240, advanced_complete_share=0.5)
    )
    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-04-25",
    )
    metadata = json.loads((result.artifact_dir / "training_metadata.json").read_text())
    assert metadata["use_median_imputation"] is True
    assert metadata["advanced_only_active"] is False
    assert metadata["advanced_sample_weight"] == 3.0
    counts = metadata["advanced_completeness_counts"]
    assert counts["__total__"] > 0
    # Median for ts_pct was learned from advanced-complete rows.
    assert metadata["advanced_feature_medians"]["ts_pct"] != 0.0


def test_training_uniform_weights_when_advanced_only_active(tmp_path):
    """When a family crosses the advanced_only_threshold, the dataset is
    filtered to advanced-complete rows and weights drop to uniform.
    Median imputation stays on (per-key coverage within advanced rows is
    still sparse) — only weighting collapses."""
    frame = settled_predictions_from_records(
        _records(total=240, advanced_only_for_family="mlb_props", advanced_only_count=120)
    )
    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-04-26",
        advanced_only_threshold=100,  # ≤ 120 mlb_props complete rows
    )
    metadata = json.loads((result.artifact_dir / "training_metadata.json").read_text())
    assert metadata["advanced_only_active"] is True
    # Median imputation is independent of advanced_only mode now.
    assert metadata["use_median_imputation"] is True
    assert metadata["advanced_sample_weight"] == 1.0
    # Training rows are now filtered to the advanced-complete subset.
    assert metadata["training_rows"] <= 120


def test_training_promotion_gate_keeps_shadow_when_baseline_beats_candidate(tmp_path):
    """A baseline brier of 0.0 (impossibly tight) forces shadow mode."""
    frame = settled_predictions_from_records(_records(total=240, advanced_complete_share=0.3))
    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-04-27",
        promotion_baseline_brier=0.0,
    )
    metadata = json.loads((result.artifact_dir / "training_metadata.json").read_text())
    assert metadata["promotion"]["promoted"] is False
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["serving_mode"] == "shadow"
    assert manifest["families"][0]["mode"] == "shadow"


def test_training_promotion_gate_promotes_when_candidate_beats_baseline(tmp_path):
    """A baseline brier of 1.0 (impossibly loose) flips serving_mode."""
    frame = settled_predictions_from_records(_records(total=240, advanced_complete_share=0.3))
    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-04-28",
        promotion_baseline_brier=1.0,
    )
    metadata = json.loads((result.artifact_dir / "training_metadata.json").read_text())
    assert metadata["promotion"]["promoted"] is True
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["serving_mode"] == "serving"
    assert manifest["families"][0]["mode"] == "serving"


def test_training_promotion_gate_does_not_promote_on_tie(tmp_path):
    """The promotion gate is strictly less-than: a baseline equal to the
    candidate's time-split brier keeps the model in shadow."""
    frame = settled_predictions_from_records(_records(total=240, advanced_complete_share=0.3))
    # First training pass — no baseline, just to discover this dataset's
    # candidate brier.
    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts1",
        manifest_out=tmp_path / "manifests1" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-04-30",
    )
    metadata = json.loads((result.artifact_dir / "training_metadata.json").read_text())
    candidate_brier = metadata["promotion"]["candidate_brier"]

    # Re-train with the baseline set EXACTLY to the candidate brier — a tie
    # must keep us in shadow.
    result_tie = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts2",
        manifest_out=tmp_path / "manifests2" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-05-01",
        promotion_baseline_brier=candidate_brier,
    )
    tie_metadata = json.loads((result_tie.artifact_dir / "training_metadata.json").read_text())
    assert tie_metadata["promotion"]["promoted"] is False
    tie_manifest = json.loads(result_tie.manifest_path.read_text())
    assert tie_manifest["serving_mode"] == "shadow"


def test_training_no_baseline_stays_shadow(tmp_path):
    """Without a baseline_brier, default behaviour is shadow."""
    frame = settled_predictions_from_records(_records(total=240, advanced_complete_share=0.3))
    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-04-29",
    )
    metadata = json.loads((result.artifact_dir / "training_metadata.json").read_text())
    assert metadata["promotion"]["promoted"] is False
    assert metadata["promotion"]["baseline_brier"] is None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["serving_mode"] == "shadow"
