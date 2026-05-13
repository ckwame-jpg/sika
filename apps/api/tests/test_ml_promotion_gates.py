import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.models import ModelFamilyRuntimeHealth, ParlayPrediction, Prediction, ShadowInference, ShadowParlayInference
from app.services.ml.promotion import (
    MIN_WALK_FORWARD_ROWS_PER_FOLD,
    MIN_WALK_FORWARD_VALID_FOLDS,
    PromotionExample,
    PromotionMetrics,
    _walk_forward_buckets,
    evaluate_family,
    evaluate_promotion_gates,
    metrics_for_examples,
)
from app.services.ml.runtime import resolve_family_runtime


# Bug #20 — promotion gate consumes the worst-fold walk-forward Brier.
# Existing seeders spaced rows 1 minute apart (single-day window) which
# cannot form a walk-forward fold; bump every seeded row to a unique
# bucket-friendly timestamp by passing ``time_step`` to the seed helper.
WALK_FORWARD_TIME_STEP = timedelta(hours=6)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_nba_shadow_pair(
    db_session,
    *,
    index: int,
    won: bool,
    shadow_probability: float,
    heuristic_probability: float = 0.52,
    time_step: timedelta = WALK_FORWARD_TIME_STEP,
) -> None:
    captured_at = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc) + index * time_step
    prediction = Prediction(
        run_id=1,
        event_id=None,
        market_id=index + 1,
        ticker=f"NBA-PROMO-{index}",
        sport_key="NBA",
        event_name="Promotion test game",
        market_title="Promotion test market",
        market_family="winner",
        market_kind="game_winner",
        capture_scope="recommendation",
        side="yes",
        action="buy",
        suggested_price=0.5,
        fair_yes_price=heuristic_probability,
        fair_no_price=round(1 - heuristic_probability, 4),
        edge=round(heuristic_probability - 0.5, 4),
        confidence=heuristic_probability,
        selection_score=0.1,
        model_name="heuristic-v1",
        rationale="Promotion test",
        reasons=["test"],
        features={"family_key": "nba_singles"},
        scoring_diagnostics={},
        market_status_at_capture="active",
        settlement_status="settled",
        prediction_outcome="won" if won else "lost",
        settled_at=captured_at + timedelta(hours=3),
        realized_pnl=0.5 if won else -0.5,
        captured_at=captured_at,
    )
    db_session.add(prediction)
    db_session.flush()
    db_session.add(
        ShadowInference(
            run_id=1,
            source_prediction_id=prediction.id,
            event_id=prediction.event_id,
            market_id=prediction.market_id,
            ticker=prediction.ticker,
            sport_key=prediction.sport_key,
            event_name=prediction.event_name,
            market_title=prediction.market_title,
            market_family=prediction.market_family,
            market_kind=prediction.market_kind,
            inference_scope="single",
            recommended_side="yes",
            suggested_price=prediction.suggested_price,
            fair_yes_price=shadow_probability,
            fair_no_price=round(1 - shadow_probability, 4),
            edge=round(shadow_probability - prediction.suggested_price, 4),
            confidence=shadow_probability,
            model_name="shadow-model",
            model_version="v1",
            calibration_version="cal-v1",
            feature_set_version="features-v1",
            model_metadata={"family_key": "nba_singles"},
            rationale="Shadow test",
            reasons=["shadow"],
            features={},
            captured_at=captured_at,
        )
    )


def _seed_promotion_ready_family(db_session, *, total: int = 320) -> None:
    """Seed enough shadow pairs to clear both volume and walk-forward gates.

    320 rows × 6-hour spacing = 80 days = ~11.4 weeks → 10 valid weekly
    walk-forward folds with ~28 rows each, comfortably above the 8-fold
    × 25-rows floor.
    """
    for index in range(total):
        won = index % 2 == 0
        _seed_nba_shadow_pair(
            db_session,
            index=index,
            won=won,
            shadow_probability=0.85 if won else 0.15,
        )
    db_session.flush()


def _write_static_manifest(tmp_path, *, mode: str = "shadow"):
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(
        json.dumps(
            {
                "family_key": "nba_singles",
                "scope": "single",
                "behavior": "static_probability",
                "probability": 0.61,
                "confidence": 0.61,
                "metadata": {"source": "promotion-test"},
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "promotion-test",
                "serving_mode": "ml",
                "families": [
                    {
                        "family_key": "nba_singles",
                        "model_name": "nba_singles-model",
                        "model_version": "v1",
                        "calibration_version": "cal-v1",
                        "feature_set_version": "features-v1",
                        "artifact_path": str(artifact_path),
                        "mode": mode,
                        "metadata": {"source": "manifest"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_promotion_gates_require_volume_calibration_ranking_and_stability():
    passing = PromotionMetrics(
        sample_count=150,
        heuristic_brier=0.24,
        shadow_brier=0.2,
        heuristic_top_decile_roi=0.01,
        shadow_top_decile_roi=0.08,
    )

    assert not evaluate_promotion_gates(passing, previous_stability_days=1).promoted
    assert evaluate_promotion_gates(passing, previous_stability_days=2).promoted
    assert not evaluate_promotion_gates(
        PromotionMetrics(149, 0.24, 0.2, 0.01, 0.08),
        previous_stability_days=2,
    ).volume_passed
    assert not evaluate_promotion_gates(
        PromotionMetrics(150, 0.24, 0.26, 0.01, 0.08),
        previous_stability_days=2,
    ).calibration_passed
    assert not evaluate_promotion_gates(
        PromotionMetrics(150, 0.24, 0.2, 0.08, 0.01),
        previous_stability_days=2,
    ).ranking_passed


def test_evaluate_family_promotes_after_three_passing_daily_evaluations(db_session):
    _seed_promotion_ready_family(db_session)

    first = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 21, tzinfo=timezone.utc))
    second = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 22, tzinfo=timezone.utc))
    third = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 23, tzinfo=timezone.utc))

    assert first.gates.stability_days == 1
    assert second.gates.stability_days == 2
    assert third.gates.promoted is True
    runtime_row = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one()
    assert runtime_row.promotion_mode == "ml"
    # Bug #20 / codex round 4: kill switch compares its 50-row rolling
    # aggregate Brier against this baseline; storing the aggregate
    # (rather than worst-fold) keeps the comparison apples-to-apples.
    assert runtime_row.promotion_baseline_brier == third.metrics.aggregate_shadow_brier


def test_runtime_uses_promotion_mode_below_explicit_family_override(db_session, monkeypatch, tmp_path):
    _seed_promotion_ready_family(db_session)
    evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 21, tzinfo=timezone.utc))
    evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 22, tzinfo=timezone.utc))
    evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 23, tzinfo=timezone.utc))
    manifest_path = _write_static_manifest(tmp_path, mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.delenv("ML_FAMILY_MODES_JSON", raising=False)
    get_settings.cache_clear()

    promoted = resolve_family_runtime(db_session, "nba_singles", scope="single")
    assert promoted.desired_mode == "ml"
    assert promoted.effective_mode == "ml"

    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "shadow"}))
    get_settings.cache_clear()
    overridden = resolve_family_runtime(db_session, "nba_singles", scope="single")
    assert overridden.desired_mode == "shadow"
    assert overridden.effective_mode == "shadow"


# -----------------------------------------------------------------------------
# Bug #20 — walk-forward worst-fold gate
#
# The promotion gate now consumes the worst-per-week-bucket Brier, with a
# per-family floor of 8 valid folds × ≥25 rows. Below that floor the
# family stays in shadow regardless of the aggregate metric — preventing
# one favourable stretch from short-circuiting the gate.


def _example(*, day_offset: int, target: int, shadow_probability: float, heuristic_probability: float = 0.5) -> PromotionExample:
    base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    return PromotionExample(
        target=target,
        heuristic_probability=heuristic_probability,
        shadow_probability=shadow_probability,
        market_price=0.5,
        realized_pnl=0.5 if target == 1 else -0.5,
        captured_at=base + timedelta(days=day_offset),
    )


def test_walk_forward_buckets_groups_by_weekly_window_when_volume_clears_floor():
    """30 rows across 10 weekly buckets → 10 valid weekly folds."""
    examples = [
        _example(day_offset=week * 7 + (row % 7), target=row % 2, shadow_probability=0.6)
        for week in range(10)
        for row in range(30)
    ]
    buckets, meta = _walk_forward_buckets(examples)
    assert meta["insufficient_history"] is False
    assert meta["window_days"] == 7
    assert meta["fold_count"] == 10
    for bucket in buckets:
        assert len(bucket) >= MIN_WALK_FORWARD_ROWS_PER_FOLD


def test_walk_forward_buckets_widen_to_biweekly_for_low_volume_families():
    """~14 rows/week → weekly buckets all drop, biweekly clears the floor."""
    examples: list[PromotionExample] = []
    for week in range(18):
        for row in range(14):
            examples.append(
                _example(day_offset=week * 7 + (row % 7), target=row % 2, shadow_probability=0.6)
            )
    buckets, meta = _walk_forward_buckets(examples)
    assert meta["insufficient_history"] is False
    assert meta["window_days"] == 14
    assert meta["fold_count"] >= MIN_WALK_FORWARD_VALID_FOLDS
    for bucket in buckets:
        assert len(bucket) >= MIN_WALK_FORWARD_ROWS_PER_FOLD


def test_walk_forward_buckets_insufficient_history_for_single_day_span():
    """All rows landing in one bucket → no folds form."""
    examples = [
        _example(day_offset=0, target=i % 2, shadow_probability=0.55) for i in range(200)
    ]
    buckets, meta = _walk_forward_buckets(examples)
    assert meta["insufficient_history"] is True
    assert meta["fold_count"] <= 1
    if meta["fold_count"] == 1:
        # Single bucket with ≥25 rows is still below the 8-fold floor.
        assert len(buckets[0]) >= MIN_WALK_FORWARD_ROWS_PER_FOLD


def test_metrics_for_examples_reports_worst_fold_brier_not_mean():
    """One bad week + nine perfect weeks → worst-fold Brier ≈ the bad
    week, not the (~0) mean. Asserts the gate consumes the worst, not
    the average."""
    examples: list[PromotionExample] = []
    # Nine pristine weeks: shadow probability matches outcome perfectly.
    for week in range(9):
        for row in range(30):
            target = row % 2
            examples.append(
                _example(
                    day_offset=week * 7 + (row % 7),
                    target=target,
                    shadow_probability=0.999 if target == 1 else 0.001,
                    heuristic_probability=0.999 if target == 1 else 0.001,
                )
            )
    # Tenth week is catastrophically wrong — every shadow probability is
    # backwards. This single week should dominate the worst-fold Brier.
    for row in range(30):
        target = row % 2
        examples.append(
            _example(
                day_offset=9 * 7 + (row % 7),
                target=target,
                shadow_probability=0.001 if target == 1 else 0.999,
                heuristic_probability=0.999 if target == 1 else 0.001,
            )
        )
    metrics = metrics_for_examples(examples)
    assert metrics.insufficient_history is False
    assert metrics.walk_forward_fold_count == 10
    # Worst-fold shadow Brier comes from the catastrophic week (≈0.996).
    # The mean across folds would be ≈ 0.0996 — order of magnitude lower.
    # If the gate accidentally consumed the aggregate, calibration would
    # still pass; consuming the worst-fold flips it.
    assert metrics.shadow_brier > 0.9, (
        f"shadow_brier must surface the catastrophic week, got {metrics.shadow_brier}"
    )
    assert metrics.aggregate_shadow_brier < 0.2, (
        "aggregate shadow Brier (the prior gate) is much lower — confirms worst-fold matters"
    )
    # Heuristic stayed perfect across all weeks → worst-fold Brier near zero.
    assert metrics.heuristic_brier < 0.01


def test_evaluate_promotion_gates_blocks_promotion_on_insufficient_history():
    """A metrics struct flagged insufficient_history must fail
    calibration even if the headline Brier numbers would otherwise
    clear the noise band."""
    insufficient = PromotionMetrics(
        sample_count=150,
        heuristic_brier=0.24,
        shadow_brier=0.20,
        heuristic_top_decile_roi=0.01,
        shadow_top_decile_roi=0.08,
        insufficient_history=True,
        walk_forward_fold_count=2,
    )
    result = evaluate_promotion_gates(insufficient, previous_stability_days=2)
    assert result.calibration_passed is False
    assert result.promoted is False
    assert any("walk-forward" in reason for reason in result.reasons)


def test_evaluate_family_does_not_promote_when_walk_forward_insufficient(db_session):
    """Seed enough rows for volume but compressed into a single day —
    walk-forward can't form folds, so the gate stalls in shadow."""
    # Compress: time_step = 1 minute means all 200 rows land in one
    # weekly bucket. Volume gate passes (≥150) but walk-forward fails.
    for index in range(200):
        won = index % 2 == 0
        _seed_nba_shadow_pair(
            db_session,
            index=index,
            won=won,
            shadow_probability=0.85 if won else 0.15,
            time_step=timedelta(minutes=1),
        )
    db_session.flush()

    result = evaluate_family(db_session, "nba_singles", now=datetime(2026, 5, 1, tzinfo=timezone.utc))
    assert result.metrics.insufficient_history is True
    assert result.gates.promoted is False
    assert result.gates.calibration_passed is False
    runtime_row = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one()
    # promotion_baseline_brier must NOT be overwritten — only successful
    # promotions stamp the baseline.
    assert runtime_row.promotion_baseline_brier is None


def test_evaluate_family_fresh_row_defaults_to_zero_stability_no_carryover(db_session):
    """Self-review case (a): a brand-new ``ModelFamilyRuntimeHealth`` row
    with no stored ``promotion_metrics`` is the legacy-payload path's
    default. ``_previous_metric_compatible`` returns False for an empty
    dict so carryover stability resets to 0 and ``same_evaluation_date``
    forces False. The first walk-forward pass then records day 1, not 0."""
    _seed_promotion_ready_family(db_session)
    # ``_seed_promotion_ready_family`` doesn't touch
    # ``ModelFamilyRuntimeHealth``; ``evaluate_family`` will lazily
    # insert a fresh row with all defaults.
    result = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 21, tzinfo=timezone.utc))
    assert result.gates.stability_days == 1
    assert result.gates.promoted is False
    runtime_row = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one()
    assert runtime_row.promotion_stability_days == 1


def test_evaluate_family_resets_stability_when_previous_payload_predates_walk_forward(db_session):
    """Codex round 4: if the stored ``promotion_metrics`` payload was
    written by the pre-bug-#20 aggregate-Brier gate, those stability days
    were earned under different semantics and must not transfer."""
    _seed_promotion_ready_family(db_session)
    # Simulate legacy state: 2 stability days already accumulated under
    # the old gate, but the stored payload has no walk_forward marker.
    runtime_row = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one_or_none()
    if runtime_row is None:
        runtime_row = ModelFamilyRuntimeHealth(family_key="nba_singles")
        db_session.add(runtime_row)
        db_session.flush()
    runtime_row.promotion_stability_days = 2
    runtime_row.promotion_metrics = {
        "last_evaluation_date": "2026-04-19",  # different day, no walk_forward marker
        "metrics": {
            "sample_count": 200,
            "heuristic_brier": 0.22,
            "shadow_brier": 0.19,
            "heuristic_top_decile_roi": 0.01,
            "shadow_top_decile_roi": 0.08,
        },
        "gates": {},
    }
    db_session.flush()

    result = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 21, tzinfo=timezone.utc))
    # Stability counter resets to 1 (only this evaluation counts under
    # the new gate); promotion should NOT fire on this single pass even
    # though the legacy counter would have crossed the threshold.
    assert result.gates.stability_days == 1
    assert result.gates.promoted is False


def test_evaluate_family_first_walk_forward_pass_counts_after_same_day_legacy_payload(db_session):
    """Codex round 5 edge case: legacy payload was written earlier on the
    same calendar day as the first walk-forward evaluation. The naive
    reset (``previous_stability_days=0`` + ``same_evaluation_date=True``)
    leaves the gate computing ``stability_days = previous_stability_days
    = 0``, so the first new-gate pass records 0 days instead of 1. Force
    ``same_evaluation_date=False`` when the previous payload is legacy
    so the first walk-forward pass counts as day 1."""
    _seed_promotion_ready_family(db_session)
    runtime_row = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one_or_none()
    if runtime_row is None:
        runtime_row = ModelFamilyRuntimeHealth(family_key="nba_singles")
        db_session.add(runtime_row)
        db_session.flush()
    eval_date = datetime(2026, 4, 21, tzinfo=timezone.utc)
    # Legacy payload stamped on the SAME calendar day as the impending
    # walk-forward evaluation.
    runtime_row.promotion_stability_days = 2
    runtime_row.promotion_metrics = {
        "last_evaluation_date": eval_date.date().isoformat(),
        "metrics": {
            "sample_count": 200,
            "heuristic_brier": 0.22,
            "shadow_brier": 0.19,
            "heuristic_top_decile_roi": 0.01,
            "shadow_top_decile_roi": 0.08,
            # No ``walk_forward`` block — payload predates bug #20.
        },
        "gates": {},
    }
    db_session.flush()

    result = evaluate_family(db_session, "nba_singles", now=eval_date)
    # First pass under the new gate must count as day 1 (not stay at 0).
    assert result.gates.stability_days == 1
    assert result.gates.promoted is False


def test_evaluate_family_keeps_stability_when_previous_payload_already_walk_forward(db_session):
    """Companion to the reset test: when the stored payload was already
    produced by the walk-forward gate (``metric == worst_fold_brier``),
    the stability counter carries forward as before."""
    _seed_promotion_ready_family(db_session)
    runtime_row = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one_or_none()
    if runtime_row is None:
        runtime_row = ModelFamilyRuntimeHealth(family_key="nba_singles")
        db_session.add(runtime_row)
        db_session.flush()
    runtime_row.promotion_stability_days = 2
    runtime_row.promotion_metrics = {
        "last_evaluation_date": "2026-04-19",
        "metrics": {
            "sample_count": 200,
            "heuristic_brier": 0.22,
            "shadow_brier": 0.19,
            "heuristic_top_decile_roi": 0.01,
            "shadow_top_decile_roi": 0.08,
            "walk_forward": {
                "metric": "worst_fold_brier",
                "fold_count": 10,
                "insufficient_history": False,
            },
        },
        "gates": {},
    }
    db_session.flush()

    result = evaluate_family(db_session, "nba_singles", now=datetime(2026, 4, 21, tzinfo=timezone.utc))
    # Carry-over preserves the previous 2 days → this third pass promotes.
    assert result.gates.stability_days == 3
    assert result.gates.promoted is True


def _seed_parlay_shadow_pair(
    db_session,
    *,
    index: int,
    won: bool,
    shadow_probability: float,
    leg_count: int = 2,
    sport_scope: str = "NBA",
    family_key: str = "nba_parlay_2leg",
    time_step: timedelta = WALK_FORWARD_TIME_STEP,
) -> None:
    captured_at = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc) + index * time_step
    parlay = ParlayPrediction(
        run_id=1,
        leg_count=leg_count,
        sport_scope=sport_scope,
        participating_sports=[sport_scope],
        combined_market_price=0.5,
        combined_model_probability=0.55,
        american_odds="+100",
        edge=0.05,
        confidence=0.6,
        selection_score=0.1,
        model_name="heuristic-parlay",
        rationale="parlay test",
        scoring_diagnostics={},
        settlement_status="settled",
        prediction_outcome="won" if won else "lost",
        settled_at=captured_at + timedelta(hours=3),
        realized_pnl=0.5 if won else -0.5,
        captured_at=captured_at,
    )
    db_session.add(parlay)
    db_session.flush()
    db_session.add(
        ShadowParlayInference(
            run_id=1,
            source_parlay_prediction_id=parlay.id,
            leg_count=leg_count,
            sport_scope=sport_scope,
            participating_sports=[sport_scope],
            leg_tickers=[f"L{index}A", f"L{index}B"],
            combined_market_price=0.5,
            combined_model_probability=shadow_probability,
            edge=shadow_probability - 0.5,
            confidence=shadow_probability,
            model_name="shadow-parlay",
            model_version="v1",
            calibration_version="cal-v1",
            feature_set_version="features-v1",
            model_metadata={"family_key": family_key},
            rationale="shadow parlay",
            features={},
            captured_at=captured_at,
        )
    )


def test_evaluate_family_parlay_with_sparse_volume_reports_insufficient_history(db_session):
    """Codex-style second-pass catch: bug #20's walk-forward floor
    (8 folds × ≥25 rows) is steep for parlay families whose settled
    volume is typically much lower than singles. Verify the gate path
    doesn't crash on parlay examples and reports insufficient_history
    for a low-volume parlay family — protecting against degenerate
    behavior if ``combined_market_price``/``combined_model_probability``
    were nullable / defaulted in ways that broke the Brier compute."""
    # 20 parlay rows total — well below the 200-row floor and the
    # ≥25-rows-per-week × ≥8-folds requirement. Spread across 5 days
    # so they all land in a single weekly bucket.
    for index in range(20):
        won = index % 2 == 0
        _seed_parlay_shadow_pair(
            db_session,
            index=index,
            won=won,
            shadow_probability=0.7 if won else 0.3,
        )
    db_session.flush()

    result = evaluate_family(db_session, "nba_parlay_2leg", now=datetime(2026, 5, 1, tzinfo=timezone.utc))
    assert result.metrics.sample_count == 20
    assert result.metrics.insufficient_history is True
    assert result.gates.calibration_passed is False
    assert result.gates.promoted is False
    # The walk_forward block must surface the actual fold-build outcome
    # for ops visibility — not a degenerate empty / nan result.
    walk_forward_payload = result.metrics.to_dict()["walk_forward"]
    assert walk_forward_payload["fold_count"] <= 1
    assert walk_forward_payload["insufficient_history"] is True


def test_promotion_metrics_to_dict_emits_none_for_empty_aggregates():
    """When no examples are seeded, aggregate Brier returns 0.0 from
    ``brier_score([])`` — emit None in ``to_dict`` so downstream
    consumers don't mistake the empty case for a real zero-Brier
    signal."""
    metrics = metrics_for_examples([])
    payload = metrics.to_dict()
    assert payload["sample_count"] == 0
    assert payload["walk_forward"]["aggregate_heuristic_brier"] is None
    assert payload["walk_forward"]["aggregate_shadow_brier"] is None


def test_promotion_metrics_to_dict_exposes_walk_forward_block():
    """Downstream UI / readiness panel inspects ``walk_forward`` payload."""
    metrics = PromotionMetrics(
        sample_count=200,
        heuristic_brier=0.20,
        shadow_brier=0.18,
        heuristic_top_decile_roi=0.05,
        shadow_top_decile_roi=0.07,
        walk_forward_fold_count=9,
        walk_forward_window_days=7,
        walk_forward_rows_per_fold=(28, 29, 30, 31, 28, 27, 32, 30, 29),
        insufficient_history=False,
        aggregate_heuristic_brier=0.21,
        aggregate_shadow_brier=0.19,
    )
    payload = metrics.to_dict()
    walk_forward = payload["walk_forward"]
    assert walk_forward["fold_count"] == 9
    assert walk_forward["window_days"] == 7
    assert walk_forward["metric"] == "worst_fold_brier"
    assert walk_forward["insufficient_history"] is False
    assert walk_forward["aggregate_shadow_brier"] == 0.19
