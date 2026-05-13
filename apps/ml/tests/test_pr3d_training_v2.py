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
    MIN_WALK_FORWARD_ROWS_PER_FOLD,
    MIN_WALK_FORWARD_VALID_FOLDS,
    _advanced_completeness_counts,
    _advanced_completeness_mask,
    _build_sample_weights,
    _compute_feature_medians,
    _fold_feature_spec,
    _row_is_advanced_complete,
    _walk_forward_folds,
    build_feature_spec,
    train_and_package,
    walk_forward_evaluation,
)


def _records(
    total: int = 240,
    *,
    advanced_complete_share: float = 0.0,
    advanced_only_for_family: str | None = None,
    advanced_only_count: int = 0,
    time_step: timedelta = timedelta(minutes=1),
):
    """Generate a synthetic settled-predictions dataset.

    ``advanced_complete_share`` — fraction of rows that get
    ``advanced_data_complete=1.0`` and a real ``ts_pct`` value.

    ``advanced_only_for_family`` + ``advanced_only_count`` — overrides the
    share to seed enough advanced-complete rows under a specific family
    for the threshold-trigger test.

    ``time_step`` — per-row gap in ``captured_at``. Default is one-minute
    spacing for legacy tests that only need a chronological ordering;
    walk-forward promotion tests pass a larger step (typically 6 hours)
    so the dataset spans the 8+ weekly folds required by the bug-#20
    walk-forward gate.
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
                "settled_at": (base + (index + 1) * time_step + timedelta(hours=3)).isoformat(),
                "realized_pnl": 0.56 if won else -0.44,
                "captured_at": (base + index * time_step).isoformat(),
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


def test_advanced_completeness_markers_match_api_emitters():
    """Scan ``apps/api/app/services`` for every ``*_data_complete`` write
    and assert the constant covers them. Catches drift when a new emitter
    lands but isn't added to ADVANCED_COMPLETENESS_MARKERS — without this
    scan, those rows would silently get sample weight 1.0 instead of 3.0
    and would be filtered OUT in advanced_only mode.
    """
    import re
    from pathlib import Path

    api_services_root = Path(__file__).resolve().parents[3] / "apps" / "api" / "app" / "services"
    assert api_services_root.is_dir(), f"expected API services dir at {api_services_root}"

    # Match any ``*_data_complete`` key written into a dict that ends up
    # in features. Three shapes show up in the codebase:
    #   1. ``out["foo_data_complete"] = 1.0`` (most emitters)
    #   2. ``"foo_data_complete": 1.0`` (dict-literal returns)
    #   3. ``"foo_data_complete": float(...)`` (park factors compute the
    #      flag from a payload field rather than the literal 1.0)
    write_patterns = (
        re.compile(r'\[\s*[\'"](\w+_data_complete)[\'"]\s*\]\s*=\s*'),
        re.compile(r'[\'"](\w+_data_complete)[\'"]\s*:\s*'),
    )
    discovered: set[str] = set()
    for py_file in api_services_root.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for pattern in write_patterns:
            for match in pattern.finditer(text):
                discovered.add(match.group(1))

    # Filter out the synthetic "_data_complete" key embedded inside park
    # factors (it's a payload field, not a feature key) — emit_park_features
    # exposes it as ``park_data_complete`` which is what we actually track.
    discovered.discard("_data_complete")

    declared = set(ADVANCED_COMPLETENESS_MARKERS)
    missing = discovered - declared
    extra = declared - discovered
    assert not missing, (
        f"ADVANCED_COMPLETENESS_MARKERS missing API emitters: {sorted(missing)}. "
        "Add them to the constant in training.py so weighting + advanced_only mode pick up these rows."
    )
    assert not extra, (
        f"ADVANCED_COMPLETENESS_MARKERS lists keys with no API emitter: {sorted(extra)}."
    )


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


def test_build_feature_spec_skips_binary_keys_during_imputation():
    """One-hot / boolean indicators (e.g. ``sport_is_nba``,
    ``has_team_context``) have training values exclusively in {0.0, 1.0}.
    Median imputation would set their default to 0.5 — a value the model
    never sees during training and that is fed at inference whenever the
    API doesn't emit the key (which is always for ``sport_is_*``). Verify
    these keys keep the historical 0.0 default."""
    frame = settled_predictions_from_records(
        _records(total=60, advanced_complete_share=0.5)
    )
    spec = build_feature_spec(frame, version="public-feature-set-v2", use_median_imputation=True)
    # ``sport_is_nba`` / ``sport_is_mlb`` are written by ``_records`` via
    # the dataset prep layer — they're guaranteed binary. Defaults must
    # stay at 0.0 (the unseen-coordinate-of-an-indicator is "off").
    assert spec.default_values["sport_is_nba"] == 0.0
    assert spec.default_values["sport_is_mlb"] == 0.0
    # ``has_team_context`` in the synthetic features is always True (1.0),
    # so it's binary-only by virtue of being a constant True. It must
    # also keep the 0.0 default (the model would otherwise see 1.0
    # everywhere AND the absent default would silently shift to 1.0).
    assert spec.default_values["has_team_context"] == 0.0


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


def test_fit_estimator_routes_sample_weight_to_pipeline_classifier_step():
    """The reviewer-flagged candidate-selection asymmetry: pre-fix,
    Pipeline candidates (LR) silently dropped ``sample_weight`` while
    HGBC accepted it. Now both estimators receive the weights via the
    appropriate routing — Pipeline gets ``<step>__sample_weight``, HGBC
    gets unprefixed.

    Verified directly by spying on the LR step's ``fit``: assert it was
    called with the same ``sample_weight`` array we passed in.
    """
    from ml.training import _candidate_estimators, _fit_estimator

    rng = np.random.default_rng(42)
    x_train = rng.normal(size=(50, 3))
    y_train = (rng.random(50) > 0.5).astype(int)
    weights = rng.uniform(0.5, 3.0, size=50)

    pipeline = _candidate_estimators(len(y_train))["logistic_regression"]
    classifier_step = pipeline.steps[-1][0]  # 'logisticregression'
    classifier = pipeline.named_steps[classifier_step]
    original_fit = classifier.fit
    captured: dict[str, Any] = {}

    def spy_fit(X, y, **kwargs):
        captured["sample_weight"] = kwargs.get("sample_weight")
        return original_fit(X, y, **kwargs)

    classifier.fit = spy_fit  # type: ignore[method-assign]
    _fit_estimator(pipeline, x_train, y_train, sample_weight=weights)

    assert "sample_weight" in captured, "LR step.fit was never called"
    assert captured["sample_weight"] is not None, (
        "LR step.fit received no sample_weight — Pipeline routing is broken"
    )
    np.testing.assert_array_equal(captured["sample_weight"], weights)


def test_fit_estimator_routes_sample_weight_to_hgbc_directly():
    """Companion to the LR test — confirms HGBC (non-Pipeline) still
    receives sample_weight via the unprefixed path."""
    from ml.training import _candidate_estimators, _fit_estimator

    rng = np.random.default_rng(7)
    x_train = rng.normal(size=(50, 3))
    y_train = (rng.random(50) > 0.5).astype(int)
    weights = rng.uniform(0.5, 3.0, size=50)

    hgbc = _candidate_estimators(len(y_train))["hist_gradient_boosting"]
    original_fit = hgbc.fit
    captured: dict[str, Any] = {}

    def spy_fit(X, y, **kwargs):
        captured["sample_weight"] = kwargs.get("sample_weight")
        return original_fit(X, y, **kwargs)

    hgbc.fit = spy_fit  # type: ignore[method-assign]
    _fit_estimator(hgbc, x_train, y_train, sample_weight=weights)

    assert captured["sample_weight"] is not None
    np.testing.assert_array_equal(captured["sample_weight"], weights)


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
    assert metadata["feature_medians"]["ts_pct"] != 0.0
    # Completeness markers must NOT have been median-imputed (their median
    # would be 1.0, collapsing the column to a constant and destroying the
    # marker signal). They keep the historical 0.0 default.
    assert metadata["feature_medians"].get("advanced_data_complete", 0.0) == 0.0


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


# Bug #20 — walk-forward gate. The promotion gate consumes the
# *worst-fold* Brier across an expanding-window walk-forward eval, so
# fixtures must span enough weeks for the gate to compute ≥8 valid folds
# with ≥25 rows each. 320 rows × 6-hour steps = ~11 weeks worth of data,
# which yields 10 weekly folds of ~28 rows each — comfortably above the
# floor without blowing up runtime.
def _walk_forward_records(**overrides):
    params = {
        "total": 320,
        "time_step": timedelta(hours=6),
        "advanced_complete_share": 0.3,
    }
    params.update(overrides)
    return _records(**params)


def test_training_promotion_gate_keeps_shadow_when_baseline_beats_candidate(tmp_path):
    """A baseline brier of 0.0 (impossibly tight) forces shadow mode."""
    frame = settled_predictions_from_records(_walk_forward_records())
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
    # Walk-forward must be valid (insufficient_history False) — otherwise
    # the test is silently asserting the wrong branch.
    assert metadata["promotion"]["insufficient_history"] is False
    assert metadata["promotion"]["candidate_brier"] is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["serving_mode"] == "shadow"
    assert manifest["families"][0]["mode"] == "shadow"


def test_training_promotion_gate_promotes_when_candidate_beats_baseline(tmp_path):
    """A baseline brier of 1.0 (impossibly loose) flips serving_mode."""
    frame = settled_predictions_from_records(_walk_forward_records())
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
    assert metadata["promotion"]["insufficient_history"] is False
    assert metadata["promotion"]["fold_count"] >= 8
    assert metadata["promotion"]["metric"] == "worst_fold_brier"
    manifest = json.loads(result.manifest_path.read_text())
    # ``"ml"`` is the runtime sentinel that actually activates the model;
    # ``"serving"`` would be silently rejected by apps/api/app/services/ml/runtime.py.
    assert manifest["serving_mode"] == "ml"
    assert manifest["families"][0]["mode"] == "ml"


def test_training_promotion_gate_does_not_promote_on_tie(tmp_path):
    """Strictly less-than: a baseline equal to the candidate's worst-fold
    walk-forward Brier keeps the model in shadow."""
    frame = settled_predictions_from_records(_walk_forward_records())
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
    assert candidate_brier is not None, "Walk-forward must be valid for the tie-break test."

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


def test_promoted_manifest_uses_runtime_compatible_mode(tmp_path):
    """The runtime in apps/api/app/services/ml/runtime.py only accepts
    ``"shadow"`` or ``"ml"`` from a manifest's ``mode`` field. A promoted
    artifact MUST use one of those literals; ``"serving"`` (or anything
    else) is rejected and falls back to auto-shadow, silently no-opping
    the entire promotion path.
    """
    frame = settled_predictions_from_records(_walk_forward_records())
    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-05-02",
        promotion_baseline_brier=1.0,  # impossibly loose → promotes
    )
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["serving_mode"] in {"shadow", "ml"}
    for family in manifest["families"]:
        assert family["mode"] in {"shadow", "ml"}


def test_fold_feature_spec_uses_only_train_fold_medians():
    """Bug #16: ``_fold_feature_spec`` must compute medians using only
    the training-fold rows so that test-fold values don't leak into
    the imputation prior. Holdout Brier reported by
    ``_evaluate_candidates`` is downstream of this prior, and the
    promotion gate is downstream of that — leakage made the gate
    look more favorable than the underlying generalization."""
    # Train fold: ``foo`` clusters at 10. Test fold: ``foo`` clusters
    # at 1000. A full-dataset median would land in the middle and
    # leak the test distribution into the imputation default; a
    # train-only median stays close to 10.
    train_frame = pd.DataFrame(
        [
            {"features": {"foo": 10.0, "family_key": "fam"}, "family_key": "fam", "ticker": "T1"},
            {"features": {"foo": 11.0, "family_key": "fam"}, "family_key": "fam", "ticker": "T2"},
            {"features": {"foo": 9.0, "family_key": "fam"}, "family_key": "fam", "ticker": "T3"},
        ]
    )
    full_frame = pd.concat(
        [
            train_frame,
            pd.DataFrame(
                [
                    {"features": {"foo": 1000.0, "family_key": "fam"}, "family_key": "fam", "ticker": "T4"},
                    {"features": {"foo": 1010.0, "family_key": "fam"}, "family_key": "fam", "ticker": "T5"},
                ]
            ),
        ],
        ignore_index=True,
    )
    base_spec = build_feature_spec(full_frame, version="v-test", use_median_imputation=True)
    fold_spec = _fold_feature_spec(train_frame, base_spec, use_median_imputation=True)

    # Same schema (keys + family list) but train-only medians.
    assert fold_spec.ordered_keys == base_spec.ordered_keys
    assert fold_spec.family_one_hot_keys == base_spec.family_one_hot_keys
    train_median = float(np.median([10.0, 11.0, 9.0]))
    full_median = float(np.median([10.0, 11.0, 9.0, 1000.0, 1010.0]))
    assert fold_spec.default_values["foo"] == train_median
    assert base_spec.default_values["foo"] == full_median
    assert fold_spec.default_values["foo"] != base_spec.default_values["foo"], (
        "fold spec must differ from full-dataset spec — otherwise the leak persists"
    )


def test_fold_feature_spec_passthrough_when_imputation_disabled():
    """When ``use_median_imputation=False`` is requested at the
    training call, ``_fold_feature_spec`` must return the base spec
    unchanged — the 0.0 defaults from ``build_feature_spec`` are the
    desired prior in that mode, and recomputing per-fold would
    silently re-introduce medians."""
    frame = pd.DataFrame(
        [
            {"features": {"foo": 10.0, "family_key": "fam"}, "family_key": "fam", "ticker": "T1"},
        ]
    )
    base_spec = build_feature_spec(frame, version="v-test", use_median_imputation=False)
    fold_spec = _fold_feature_spec(frame, base_spec, use_median_imputation=False)
    assert fold_spec is base_spec


def test_training_no_baseline_stays_shadow(tmp_path):
    """Without a baseline_brier, default behaviour is shadow."""
    frame = settled_predictions_from_records(_walk_forward_records())
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
    assert metadata["promotion"]["candidate_brier"] is not None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["serving_mode"] == "shadow"


# -----------------------------------------------------------------------------
# Bug #20 — walk-forward fold builder + worst-fold gate
#
# These tests exercise the expanding-window walk-forward eval directly,
# plus the promotion-gate semantics that consume its output. The
# fold-builder is purely time-ordered, so the fixtures here use bare
# datetimes rather than full settled-prediction rows where possible.


def test_walk_forward_folds_weekly_when_volume_clears_threshold():
    """8+ weekly buckets of 30 rows each → 8 valid folds, weekly window."""
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    # Nine 7-day buckets × 30 rows = 270 rows. First bucket trains, the
    # remaining 8 become test folds — exactly the minimum.
    captured_at = []
    for bucket in range(9):
        for offset_minutes in range(30):
            captured_at.append(base + timedelta(days=bucket * 7, minutes=offset_minutes * 10))
    folds, meta = _walk_forward_folds(captured_at)
    assert meta["insufficient_history"] is False
    assert meta["week_size_days"] == 7
    assert meta["fold_count"] == 8
    assert meta["rows_per_fold"] == [30] * 8
    # First fold trains on the first bucket (30 rows) and tests on the
    # second bucket (30 rows). Indices are positions in the input list.
    train_idx, test_idx = folds[0]
    assert len(train_idx) == 30
    assert len(test_idx) == 30


def test_walk_forward_folds_widen_to_two_weeks_for_low_volume_families():
    """Weekly buckets fall short of the row floor → widen to 14-day.

    Game-winner-shaped fixture: ~12 settled picks per week. Weekly
    buckets fail the 25-row floor everywhere, so the builder retries
    with 14-day buckets where each bucket clears 24 rows.
    """
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    # 18 weeks × 12 rows/week = 216 rows. Weekly buckets: 12 rows each
    # → all dropped (< 25). Biweekly buckets: 24 rows each → still < 25
    # AT 12 rows/week. Use 14 rows/week to make biweekly succeed at 28
    # rows/bucket.
    captured_at = []
    for week in range(18):
        for offset in range(14):
            captured_at.append(base + timedelta(days=week * 7, hours=offset * 12))
    folds, meta = _walk_forward_folds(captured_at)
    assert meta["insufficient_history"] is False
    assert meta["week_size_days"] == 14
    assert meta["fold_count"] >= MIN_WALK_FORWARD_VALID_FOLDS
    for row_count in meta["rows_per_fold"]:
        assert row_count >= MIN_WALK_FORWARD_ROWS_PER_FOLD


def test_walk_forward_folds_insufficient_history_when_too_compressed():
    """Single-day fixture → no weekly bucket can form, insufficient_history."""
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    captured_at = [base + timedelta(minutes=i) for i in range(200)]
    folds, meta = _walk_forward_folds(captured_at)
    assert meta["insufficient_history"] is True
    # All rows land in bucket 0 → no test folds form at either window.
    assert meta["fold_count"] == 0
    assert folds == []


def test_walk_forward_folds_drops_buckets_below_row_floor():
    """Weekly buckets with <25 rows are not test folds, but their rows
    still contribute to training slices of later folds — they are real
    settled data, just not enough on their own to form a test fold."""
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    captured_at: list[datetime] = []
    # Bucket 0: 30 rows.
    captured_at.extend(base + timedelta(days=0, minutes=i * 10) for i in range(30))
    # Bucket 1: only 10 rows → must NOT be a test fold.
    captured_at.extend(base + timedelta(days=7, minutes=i * 10) for i in range(10))
    # Buckets 2..9: 30 rows each → 8 valid test folds.
    for week in range(2, 10):
        captured_at.extend(base + timedelta(days=week * 7, minutes=i * 10) for i in range(30))
    folds, meta = _walk_forward_folds(captured_at)
    assert meta["insufficient_history"] is False
    assert meta["fold_count"] == 8, "bucket 1 (10 rows) must not become a test fold"
    assert all(rows >= MIN_WALK_FORWARD_ROWS_PER_FOLD for rows in meta["rows_per_fold"])
    # First valid test fold is bucket 2; its training slice spans
    # buckets 0 + 1 (30 + 10 rows). The under-populated bucket still
    # contributes to training — we don't throw the data away.
    train_idx, test_idx = folds[0]
    assert len(train_idx) == 40
    assert len(test_idx) == 30


def test_walk_forward_evaluation_emits_worst_fold_brier(tmp_path):
    """Train across walk-forward folds; payload exposes worst-fold Brier."""
    frame = settled_predictions_from_records(_walk_forward_records())
    spec = build_feature_spec(frame, version="v-walk-forward", use_median_imputation=True)
    payload = walk_forward_evaluation(
        frame,
        spec,
        sample_weight=None,
        use_median_imputation=True,
    )
    assert payload["insufficient_history"] is False
    assert payload["fold_count"] >= MIN_WALK_FORWARD_VALID_FOLDS
    assert payload["fold_window_days"] in {7, 14}
    for name, candidate in payload["candidates"].items():
        assert len(candidate["fold_briers"]) == payload["fold_count"]
        assert candidate["worst_fold_brier"] == max(candidate["fold_briers"]), (
            f"{name}: worst_fold_brier must be the maximum per-fold Brier"
        )
        # Mean must be ≤ worst (max), strict when folds differ. Equality
        # only when all folds yield identical Brier.
        assert candidate["mean_fold_brier"] <= candidate["worst_fold_brier"]


def test_walk_forward_evaluation_skips_fits_on_insufficient_history(tmp_path):
    """When fold-building fails, no candidate fits run."""
    # Compressed single-day timing — walk-forward must report insufficient
    # and skip the per-fold sklearn fits.
    frame = settled_predictions_from_records(_records(total=240, time_step=timedelta(minutes=1)))
    spec = build_feature_spec(frame, version="v-walk-forward-empty", use_median_imputation=True)
    payload = walk_forward_evaluation(
        frame,
        spec,
        sample_weight=None,
        use_median_imputation=True,
    )
    assert payload["insufficient_history"] is True
    assert payload["candidates"] == {}


def test_training_promotion_insufficient_history_blocks_promotion(tmp_path):
    """Insufficient walk-forward → promotion never fires even when the
    baseline is impossibly loose. Surfaces ``reason: insufficient_history``."""
    # Single-day spacing → walk-forward can't form folds.
    frame = settled_predictions_from_records(_records(total=240, time_step=timedelta(minutes=1)))
    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-05-04",
        promotion_baseline_brier=1.0,  # would normally promote
    )
    metadata = json.loads((result.artifact_dir / "training_metadata.json").read_text())
    assert metadata["promotion"]["promoted"] is False
    assert metadata["promotion"]["insufficient_history"] is True
    assert metadata["promotion"].get("reason") == "insufficient_history"
    assert metadata["promotion"]["candidate_brier"] is None
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["serving_mode"] == "shadow"


def test_training_metadata_includes_walk_forward_block(tmp_path):
    """Walk-forward observability lives in metadata['walk_forward_evaluation']."""
    frame = settled_predictions_from_records(_walk_forward_records())
    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-05-05",
    )
    metadata = json.loads((result.artifact_dir / "training_metadata.json").read_text())
    walk_forward = metadata["walk_forward_evaluation"]
    assert walk_forward["insufficient_history"] is False
    assert walk_forward["fold_count"] >= MIN_WALK_FORWARD_VALID_FOLDS
    assert walk_forward["min_rows_per_fold"] == MIN_WALK_FORWARD_ROWS_PER_FOLD
    assert walk_forward["min_valid_folds"] == MIN_WALK_FORWARD_VALID_FOLDS
    # Promotion candidate_brier = winner's worst-fold from walk-forward.
    winner = metadata["winner"]
    expected = walk_forward["candidates"][winner]["worst_fold_brier"]
    assert metadata["promotion"]["candidate_brier"] == expected
    assert metadata["promotion"]["metric"] == "worst_fold_brier"
