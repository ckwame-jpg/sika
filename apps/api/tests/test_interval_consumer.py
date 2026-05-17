"""Tests for Smarter #21 phase 2d — scoring kernel interval consumer.

Pins the pure-math helpers and the artifact-lookup path:

- ``triangular_yes_probability`` — closed-form CDF integration over a
  triangular distribution defined by (p10, p50, p90). Edge cases:
  threshold below p10, above p90, exactly at p50, and degenerate
  intervals where two or all three quantiles collapse to the same
  value.
- ``coverage_status_for_stat`` — reads
  ``<artifact_dir>/interval_models/<stat>/metadata.json`` and reuses
  ``_classify_coverage`` from interval_status.py so the consumer
  shares one source of truth with the readiness panel + CLI.
- ``consume_prediction_interval`` — end-to-end happy / fallback /
  bad-coverage paths against a tmp_path artifact + manifest.
- ``_score_player_prop`` wire-in — bottom-of-file integration tests
  that pin the swap (or non-swap) behavior of ``probability_yes``
  based on coverage_status.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.ensemble import GradientBoostingRegressor

from app.config import get_settings
from app.services.ml.artifact_loader import clear_cache
from app.services.ml.interval_status import coverage_status_for_stat
from app.services.scoring.interval_consumer import (
    consume_prediction_interval,
    triangular_yes_probability,
)


# -- Fixtures -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    get_settings.cache_clear()
    clear_cache()
    yield
    get_settings.cache_clear()
    clear_cache()


def _fit_quantile_triple(n_features: int = 2, seed: int = 2026):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(60, n_features))
    y = X[:, 0] * 2.0 + rng.normal(scale=0.5, size=60)
    return tuple(
        GradientBoostingRegressor(loss="quantile", alpha=alpha, max_depth=2, n_estimators=30).fit(X, y)
        for alpha in (0.10, 0.50, 0.90)
    )


def _seed_artifact_with_intervals(
    tmp_path: Path,
    *,
    stat_key: str,
    family_key: str = "nba_props",
    empirical_coverage: float = 0.81,
    sample_size: int = 120,
) -> Path:
    """Create a manifest + sklearn artifact + interval sidecar for one
    stat key. Returns the manifest path so tests can pin via
    ``ML_MANIFEST_PATH``."""
    from ml_features import FeatureSpec
    from sklearn.linear_model import LogisticRegression

    artifact_dir = tmp_path / "artifacts" / "global_v1_20260516"
    artifact_dir.mkdir(parents=True)

    X = np.array([[0.0, 0.0], [1.0, 1.0], [0.0, 1.0], [1.0, 0.0]])
    y = np.array([0, 1, 1, 0])
    pipeline = LogisticRegression().fit(X, y)
    joblib.dump(pipeline, artifact_dir / "model.joblib")

    spec = FeatureSpec(
        version="test-v1",
        ordered_keys=["feature_a", "feature_b"],
        default_values={"feature_a": 0.0, "feature_b": 0.0},
        family_one_hot_keys=[],
    )
    (artifact_dir / "feature_spec.json").write_text(json.dumps(spec.to_dict()))
    (artifact_dir / "training_metadata.json").write_text(json.dumps({"target_type": "yes_won"}))

    stat_dir = artifact_dir / "interval_models" / stat_key
    stat_dir.mkdir(parents=True)
    triple = _fit_quantile_triple(n_features=2)
    joblib.dump(triple[0], stat_dir / "p10.joblib")
    joblib.dump(triple[1], stat_dir / "p50.joblib")
    joblib.dump(triple[2], stat_dir / "p90.joblib")
    (stat_dir / "metadata.json").write_text(
        json.dumps({
            "family_key": family_key,
            "stat_key": stat_key,
            "quantiles": [0.1, 0.5, 0.9],
            "sample_size": sample_size,
            "empirical_coverage": empirical_coverage,
            "trained_at": "2026-05-16T20:55:07.283448+00:00",
            "window_start": "2026-04-16T00:00:00+00:00",
            "window_end": "2026-05-16T00:00:00+00:00",
        })
    )

    manifest_path = tmp_path / "manifest.json"
    rel_artifact = Path(
        os.path.relpath(artifact_dir.resolve(), manifest_path.parent.resolve())
    ).as_posix()
    manifest_path.write_text(json.dumps({
        "version": "test",
        "serving_mode": "shadow",
        "families": [
            {
                "family_key": "global_v1",
                "serves_family_key": family_key,
                "model_name": "global-model",
                "model_version": "v1",
                "artifact_path": rel_artifact,
                "mode": "ml",
            }
        ],
    }))
    return manifest_path


# -- triangular_yes_probability -----------------------------------------


def test_triangular_yes_probability_threshold_below_p10_is_near_one() -> None:
    """All probability mass is to the right of the threshold."""
    assert triangular_yes_probability(p10=10.0, p50=12.0, p90=14.0, threshold=9.5) == pytest.approx(1.0)


def test_triangular_yes_probability_threshold_above_p90_is_near_zero() -> None:
    """All probability mass is to the left of the threshold."""
    assert triangular_yes_probability(p10=10.0, p50=12.0, p90=14.0, threshold=14.5) == pytest.approx(0.0)


def test_triangular_yes_probability_threshold_at_p50_yields_half() -> None:
    """At the mode of a symmetric triangular, CDF(p50) = 0.5 so
    P(X > p50) = 0.5."""
    assert triangular_yes_probability(p10=10.0, p50=12.0, p90=14.0, threshold=12.0) == pytest.approx(0.5)


def test_triangular_yes_probability_symmetric_quarter_mark() -> None:
    """Hand-computed: symmetric triangular on (a=10, c=12, b=14),
    threshold=11. CDF(11) = (11-10)^2 / ((14-10)*(12-10)) = 1/8.
    So P(X > 11) = 7/8 = 0.875."""
    assert triangular_yes_probability(p10=10.0, p50=12.0, p90=14.0, threshold=11.0) == pytest.approx(0.875)


def test_triangular_yes_probability_symmetric_upper_quarter() -> None:
    """Hand-computed: symmetric triangular on (a=10, c=12, b=14),
    threshold=13. CDF(13) = 1 - (14-13)^2 / ((14-10)*(14-12)) = 1 - 1/8 = 7/8.
    So P(X > 13) = 1/8 = 0.125."""
    assert triangular_yes_probability(p10=10.0, p50=12.0, p90=14.0, threshold=13.0) == pytest.approx(0.125)


def test_triangular_yes_probability_skewed_distribution() -> None:
    """Right-skewed: a=10, c=11, b=20. Threshold=11 (at the mode).
    CDF(c) for triangular always = (c-a)/(b-a) = 1/10 = 0.1.
    So P(X > 11) = 0.9."""
    assert triangular_yes_probability(p10=10.0, p50=11.0, p90=20.0, threshold=11.0) == pytest.approx(0.9)


def test_triangular_yes_probability_degenerate_point_mass_above() -> None:
    """p10 == p50 == p90 collapses to a point mass. Threshold below
    the mass → P(over) = 1.0."""
    assert triangular_yes_probability(p10=12.0, p50=12.0, p90=12.0, threshold=11.0) == pytest.approx(1.0)


def test_triangular_yes_probability_degenerate_point_mass_below() -> None:
    """Threshold above the point mass → P(over) = 0.0."""
    assert triangular_yes_probability(p10=12.0, p50=12.0, p90=12.0, threshold=13.0) == pytest.approx(0.0)


def test_triangular_yes_probability_degenerate_point_mass_equal() -> None:
    """Threshold AT the point mass → 0.5 (we have no info to break
    the tie; preserves prior monotonicity)."""
    assert triangular_yes_probability(p10=12.0, p50=12.0, p90=12.0, threshold=12.0) == pytest.approx(0.5)


def test_triangular_yes_probability_left_collapsed_p10_equals_p50() -> None:
    """p10 == p50, p90 > p50 — right-skewed triangle with mode at the
    left edge. Threshold at the mode: CDF(c=a) = 0; P(over) = 1.0."""
    assert triangular_yes_probability(p10=10.0, p50=10.0, p90=14.0, threshold=10.0) == pytest.approx(1.0)
    # Threshold midway between mode and upper bound: hand-computed
    # CDF(12) for right-triangle on (a=10, b=14, mode=a) is
    # 1 - (b-x)^2/(b-a)^2 = 1 - 4/16 = 0.75; P(over) = 0.25.
    assert triangular_yes_probability(p10=10.0, p50=10.0, p90=14.0, threshold=12.0) == pytest.approx(0.25)


def test_triangular_yes_probability_right_collapsed_p50_equals_p90() -> None:
    """p50 == p90, p10 < p50 — left-skewed triangle with mode at the
    right edge. Threshold at midpoint: hand-computed CDF(12) for left-
    triangle on (a=10, b=14, mode=b) is (x-a)^2/(b-a)^2 = 4/16 = 0.25;
    P(over) = 0.75."""
    assert triangular_yes_probability(p10=10.0, p50=14.0, p90=14.0, threshold=12.0) == pytest.approx(0.75)


def test_triangular_yes_probability_rejects_unsorted_input() -> None:
    """Defensive: caller is contracted to pass monotonized quantiles
    (apply_interval_models sorts them). Bare math helper still
    asserts the contract."""
    with pytest.raises(ValueError):
        triangular_yes_probability(p10=14.0, p50=12.0, p90=10.0, threshold=12.0)


# -- coverage_status_for_stat ------------------------------------------


def test_coverage_status_for_stat_ok(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "art"
    (artifact_dir / "interval_models" / "points").mkdir(parents=True)
    (artifact_dir / "interval_models" / "points" / "metadata.json").write_text(
        json.dumps({"empirical_coverage": 0.80})
    )
    assert coverage_status_for_stat(artifact_dir, "points") == "ok"


def test_coverage_status_for_stat_warn(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "art"
    (artifact_dir / "interval_models" / "rebounds").mkdir(parents=True)
    (artifact_dir / "interval_models" / "rebounds" / "metadata.json").write_text(
        json.dumps({"empirical_coverage": 0.92})
    )
    assert coverage_status_for_stat(artifact_dir, "rebounds") == "warn"


def test_coverage_status_for_stat_bad(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "art"
    (artifact_dir / "interval_models" / "made_threes").mkdir(parents=True)
    (artifact_dir / "interval_models" / "made_threes" / "metadata.json").write_text(
        json.dumps({"empirical_coverage": 0.96})
    )
    assert coverage_status_for_stat(artifact_dir, "made_threes") == "bad"


def test_coverage_status_for_stat_unknown_when_metadata_missing(tmp_path: Path) -> None:
    """Missing metadata.json -> ``unknown`` (visible, not silent
    fallback to ok)."""
    artifact_dir = tmp_path / "art"
    (artifact_dir / "interval_models" / "assists").mkdir(parents=True)
    assert coverage_status_for_stat(artifact_dir, "assists") == "unknown"


def test_coverage_status_for_stat_unknown_when_coverage_field_missing(tmp_path: Path) -> None:
    """metadata.json exists but no empirical_coverage key -> unknown."""
    artifact_dir = tmp_path / "art"
    (artifact_dir / "interval_models" / "hits").mkdir(parents=True)
    (artifact_dir / "interval_models" / "hits" / "metadata.json").write_text(
        json.dumps({"stat_key": "hits"})
    )
    assert coverage_status_for_stat(artifact_dir, "hits") == "unknown"


def test_coverage_status_for_stat_unknown_when_stat_dir_missing(tmp_path: Path) -> None:
    """No subdirectory for the stat key -> unknown."""
    artifact_dir = tmp_path / "art"
    artifact_dir.mkdir()
    assert coverage_status_for_stat(artifact_dir, "points") == "unknown"


# -- consume_prediction_interval ---------------------------------------


def test_consume_prediction_interval_returns_none_when_no_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No manifest at the configured path -> ``None``. We pin the
    setting to a non-existent path so the bundled-default fallback
    doesn't pull in the live ``apps/ml/manifests/current.json``."""
    missing_manifest = tmp_path / "no-such-manifest.json"
    monkeypatch.setenv("ML_MANIFEST_PATH", str(missing_manifest))
    get_settings.cache_clear()
    result = consume_prediction_interval(
        family_key="nba_props",
        stat_key="points",
        threshold=22.5,
        features={"feature_a": 0.5, "feature_b": 0.5},
        poisson_yes_probability=0.55,
    )
    assert result is None


def test_consume_prediction_interval_returns_none_when_stat_not_in_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact exists but has no interval model for this stat ->
    ``None`` (fallback path; behavior unchanged for the prop)."""
    manifest_path = _seed_artifact_with_intervals(
        tmp_path, stat_key="points", family_key="nba_props",
    )
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    get_settings.cache_clear()

    # Asking for a stat that has NO interval model.
    result = consume_prediction_interval(
        family_key="nba_props",
        stat_key="rebounds",
        threshold=10.5,
        features={"feature_a": 0.5, "feature_b": 0.5},
        poisson_yes_probability=0.55,
    )
    assert result is None


def test_consume_prediction_interval_returns_none_when_family_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Family is not in the manifest -> ``None``."""
    manifest_path = _seed_artifact_with_intervals(
        tmp_path, stat_key="points", family_key="nba_props",
    )
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    get_settings.cache_clear()

    result = consume_prediction_interval(
        family_key="mlb_props",
        stat_key="points",
        threshold=22.5,
        features={"feature_a": 0.5, "feature_b": 0.5},
        poisson_yes_probability=0.55,
    )
    assert result is None


def test_consume_prediction_interval_ok_coverage_returns_full_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path with ok coverage: diagnostic dict contains the
    (p10, p50, p90) tuple, both probabilities, delta, source tag,
    and coverage_status."""
    manifest_path = _seed_artifact_with_intervals(
        tmp_path,
        stat_key="points",
        family_key="nba_props",
        empirical_coverage=0.81,
    )
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    get_settings.cache_clear()

    result = consume_prediction_interval(
        family_key="nba_props",
        stat_key="points",
        threshold=22.5,
        features={"feature_a": 0.5, "feature_b": 0.5},
        poisson_yes_probability=0.55,
    )
    assert result is not None
    assert set(result.keys()) >= {
        "p10", "p50", "p90",
        "source", "coverage_status",
        "yes_probability_from_interval",
        "yes_probability_from_poisson",
        "delta",
        "threshold",
    }
    assert result["source"] == "interval_model_v1"
    assert result["coverage_status"] == "ok"
    assert result["yes_probability_from_poisson"] == pytest.approx(0.55)
    assert result["threshold"] == pytest.approx(22.5)
    # Monotonized triple invariant.
    assert result["p10"] <= result["p50"] <= result["p90"]
    # Interval probability is bounded [0, 1].
    assert 0.0 <= result["yes_probability_from_interval"] <= 1.0
    # Delta is interval - poisson.
    assert result["delta"] == pytest.approx(
        result["yes_probability_from_interval"] - result["yes_probability_from_poisson"]
    )


def test_consume_prediction_interval_bad_coverage_still_returns_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad coverage: diagnostic dict is STILL returned (operators
    inspect side-by-side) but coverage_status surfaces the gate
    failure so the scoring kernel knows not to swap probability_yes."""
    manifest_path = _seed_artifact_with_intervals(
        tmp_path,
        stat_key="made_threes",
        family_key="nba_props",
        empirical_coverage=0.96,
    )
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    get_settings.cache_clear()

    result = consume_prediction_interval(
        family_key="nba_props",
        stat_key="made_threes",
        threshold=1.5,
        features={"feature_a": 0.5, "feature_b": 0.5},
        poisson_yes_probability=0.62,
    )
    assert result is not None
    assert result["coverage_status"] == "bad"
    assert result["yes_probability_from_poisson"] == pytest.approx(0.62)


def test_consume_prediction_interval_returns_none_when_artifact_load_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact directory is corrupt (missing model.joblib) ->
    ``None`` (graceful fallback, scoring continues on Poisson)."""
    manifest_path = _seed_artifact_with_intervals(
        tmp_path, stat_key="points", family_key="nba_props",
    )
    # Corrupt the artifact: remove model.joblib so load_sklearn_artifact raises.
    (manifest_path.parent / "artifacts" / "global_v1_20260516" / "model.joblib").unlink()
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    get_settings.cache_clear()
    clear_cache()

    result = consume_prediction_interval(
        family_key="nba_props",
        stat_key="points",
        threshold=22.5,
        features={"feature_a": 0.5, "feature_b": 0.5},
        poisson_yes_probability=0.55,
    )
    assert result is None


# -- Scoring kernel wire-in integration --------------------------------


def _nba_prop_game_logs() -> list[dict]:
    """Ten plausible NBA game logs averaging ~30 points / 35 minutes
    so the participation gate passes and Poisson lambda > 0."""
    return [
        {
            "location": "home" if index % 2 == 0 else "away",
            "opponent": "Boston Celtics",
            "opponent_abbreviation": "BOS",
            "raw_metrics": {
                "minutes": 35.0,
                "points": 30.0,
                "rebounds": 4.0,
                "assists": 7.0,
                "steals": 1.0,
                "blocks": 0.0,
                "turnovers": 2.0,
                "field_goals_attempted": 22.0,
            },
        }
        for index in range(10)
    ]


def _seed_nba_prop_market(db_session, *, stat_key: str = "points", threshold: float = 22.5):
    """Plant the minimum DB scaffolding for ``_score_player_prop`` to
    emit a score for a player_prop market. Mirrors the helper in
    test_pr3_heuristic_audit.py but keeps interval-consumer tests
    self-contained."""
    from datetime import datetime, timezone

    from app.models import Event, EventParticipant, Market, MarketSnapshot, Participant

    home = Participant(
        external_id=f"nyk-interval-{stat_key}", sport_key="NBA",
        display_name="New York Knicks", short_name="Knicks", participant_type="team",
    )
    away = Participant(
        external_id=f"bos-interval-{stat_key}", sport_key="NBA",
        display_name="Boston Celtics", short_name="Celtics", participant_type="team",
    )
    db_session.add_all([home, away])
    db_session.flush()

    event = Event(
        external_id=f"nba-interval-prop-{stat_key}",
        sport_key="NBA",
        name="Boston Celtics at New York Knicks",
        status="scheduled",
        starts_at=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
    ])
    market = Market(
        ticker=f"KXNBA-INTERVAL-{stat_key.upper()}",
        sport_key="NBA",
        event_id=event.id,
        title=f"Jalen Brunson: {stat_key} prop",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": stat_key,
            "copilot_threshold": threshold,
            "copilot_direction": "over",
            "copilot_subject_name": "Jalen Brunson",
            "copilot_subject_team": "NYK",
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.45, no_ask=0.60, last_price=0.46)
    db_session.add_all([market, snapshot])
    db_session.commit()
    return event, market, snapshot


def test_score_player_prop_emits_prediction_interval_when_ok_coverage(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end pin: when the served family has an interval model
    with ok coverage for the prop's stat key, ``_score_player_prop``
    populates ``features["prediction_interval"]`` AND swaps
    ``probability_yes`` to the interval-derived value."""
    from app.services.scoring import PropStatsResolver, ResolvedPropSubject, _score_player_prop

    class _FakeResolver(PropStatsResolver):
        def __init__(self, resolved):
            self._resolved = resolved

        def resolve(self, sport_key, subject_name, team_hint=None):
            return self._resolved

    manifest_path = _seed_artifact_with_intervals(
        tmp_path, stat_key="points", family_key="nba_props",
        empirical_coverage=0.81,
    )
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    get_settings.cache_clear()

    event, market, snapshot = _seed_nba_prop_market(db_session, stat_key="points", threshold=22.5)
    resolved = ResolvedPropSubject(
        sport_key="NBA", athlete_id="brunson-1",
        display_name="Jalen Brunson", team_name="New York Knicks",
        season=2026, game_logs=_nba_prop_game_logs(),
        advanced_payload={}, advanced_cache_status="miss",
    )

    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    probability_yes, _confidence, _reasons, features, _feature_groups = result
    assert "prediction_interval" in features
    interval = features["prediction_interval"]
    assert interval["coverage_status"] == "ok"
    assert interval["source"] == "interval_model_v1"
    # Probability swap: probability_yes == interval-derived (not Poisson).
    assert probability_yes == pytest.approx(interval["yes_probability_from_interval"])
    # Poisson value preserved in diagnostic for A/B inspection.
    assert "yes_probability_from_poisson" in interval
    # The two are different in general (otherwise nothing changed).
    # We only assert "preserved" — both are valid probabilities.
    assert 0.0 <= interval["yes_probability_from_poisson"] <= 1.0


def test_score_player_prop_emits_prediction_interval_without_swap_on_bad_coverage(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage-status gating: when the interval's coverage is ``bad``,
    the diagnostic is STILL surfaced (operators inspect side-by-side)
    but ``probability_yes`` stays on the Poisson value."""
    from app.services.scoring import PropStatsResolver, ResolvedPropSubject, _score_player_prop

    class _FakeResolver(PropStatsResolver):
        def __init__(self, resolved):
            self._resolved = resolved

        def resolve(self, sport_key, subject_name, team_hint=None):
            return self._resolved

    manifest_path = _seed_artifact_with_intervals(
        tmp_path, stat_key="points", family_key="nba_props",
        empirical_coverage=0.96,  # bad band (over-covering)
    )
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    get_settings.cache_clear()

    event, market, snapshot = _seed_nba_prop_market(db_session, stat_key="points", threshold=22.5)
    resolved = ResolvedPropSubject(
        sport_key="NBA", athlete_id="brunson-2",
        display_name="Jalen Brunson", team_name="New York Knicks",
        season=2026, game_logs=_nba_prop_game_logs(),
        advanced_payload={}, advanced_cache_status="miss",
    )

    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    probability_yes, _confidence, _reasons, features, _feature_groups = result
    assert "prediction_interval" in features
    interval = features["prediction_interval"]
    # The real invariant: bad coverage means no swap, so
    # probability_yes still equals the Poisson value. (The
    # asserted-different check vs interval-derived was dropped
    # because coincidental equality across sklearn versions could
    # produce a spurious failure — the coverage_status assertion
    # above already proves no swap path was taken.) The stored
    # Poisson value is rounded to 4dp in the diagnostic, so allow
    # 5e-5 tolerance for the round.
    assert interval["coverage_status"] == "bad"
    assert probability_yes == pytest.approx(
        interval["yes_probability_from_poisson"], abs=5e-5,
    )


def test_score_player_prop_no_interval_diagnostic_when_stat_not_trained(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing behavior unchanged for stat keys that don't have an
    interval model: no ``prediction_interval`` key in features, no
    probability change."""
    from app.services.scoring import PropStatsResolver, ResolvedPropSubject, _score_player_prop

    class _FakeResolver(PropStatsResolver):
        def __init__(self, resolved):
            self._resolved = resolved

        def resolve(self, sport_key, subject_name, team_hint=None):
            return self._resolved

    # Artifact has interval model for "rebounds" only; prop is for "points".
    manifest_path = _seed_artifact_with_intervals(
        tmp_path, stat_key="rebounds", family_key="nba_props",
        empirical_coverage=0.81,
    )
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    get_settings.cache_clear()

    event, market, snapshot = _seed_nba_prop_market(db_session, stat_key="points", threshold=22.5)
    resolved = ResolvedPropSubject(
        sport_key="NBA", athlete_id="brunson-3",
        display_name="Jalen Brunson", team_name="New York Knicks",
        season=2026, game_logs=_nba_prop_game_logs(),
        advanced_payload={}, advanced_cache_status="miss",
    )

    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _probability_yes, _confidence, _reasons, features, _feature_groups = result
    assert "prediction_interval" not in features
