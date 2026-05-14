"""Unit tests for the reliability-curve bucketing helpers in readiness.py.

Smarter #1 (per-family, per-price-bucket calibration tracking): each row
contributes (predicted_yes_probability, did_yes_happen). Rows are bucketed by
predicted probability and we compare the bucket's average prediction to the
observed YES rate. Miscalibration = avg_predicted - actual_yes_rate (signed:
positive means the model was over-confident in YES).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.ml import readiness


@dataclass
class _Row:
    """Minimal stand-in for a Prediction / ParlayPrediction row.

    The bucketing helpers only call ``getattr`` for documented fields, so this
    dataclass is enough.
    """

    fair_yes_price: float | None = None
    combined_model_probability: float | None = None
    side: str = "yes"
    prediction_outcome: str = "won"


@dataclass
class _ParlayRow:
    """Stand-in for ``ParlayPrediction`` — no ``side`` column (each leg has
    its own side; the parent table only carries the joint probability and
    outcome)."""

    combined_model_probability: float | None = None
    prediction_outcome: str = "won"


# -- _did_yes_happen -----------------------------------------------------------


def test_did_yes_happen_yes_side_won_means_yes_happened() -> None:
    assert readiness._did_yes_happen(_Row(side="yes", prediction_outcome="won")) is True


def test_did_yes_happen_yes_side_lost_means_yes_did_not_happen() -> None:
    assert readiness._did_yes_happen(_Row(side="yes", prediction_outcome="lost")) is False


def test_did_yes_happen_no_side_won_means_yes_did_not_happen() -> None:
    # The NO side won → YES outcome is False from the market's perspective.
    assert readiness._did_yes_happen(_Row(side="no", prediction_outcome="won")) is False


def test_did_yes_happen_no_side_lost_means_yes_happened() -> None:
    # The NO side lost → YES outcome is True from the market's perspective.
    assert readiness._did_yes_happen(_Row(side="no", prediction_outcome="lost")) is True


@pytest.mark.parametrize("outcome", ["push", "cancelled", "pending"])
def test_did_yes_happen_undecided_returns_none(outcome: str) -> None:
    assert readiness._did_yes_happen(_Row(side="yes", prediction_outcome=outcome)) is None


def test_did_yes_happen_unknown_side_returns_none() -> None:
    assert readiness._did_yes_happen(_Row(side="", prediction_outcome="won")) is None


# -- _predicted_yes_probability ------------------------------------------------


def test_predicted_yes_probability_uses_fair_yes_price_for_singles() -> None:
    row = _Row(fair_yes_price=0.62)
    assert readiness._predicted_yes_probability(row) == pytest.approx(0.62)


def test_predicted_yes_probability_falls_back_to_combined_for_parlays() -> None:
    row = _Row(fair_yes_price=None, combined_model_probability=0.41)
    assert readiness._predicted_yes_probability(row) == pytest.approx(0.41)


def test_predicted_yes_probability_returns_none_when_both_missing() -> None:
    row = _Row(fair_yes_price=None, combined_model_probability=None)
    assert readiness._predicted_yes_probability(row) is None


def test_predicted_yes_probability_rejects_non_finite() -> None:
    row = _Row(fair_yes_price=float("nan"))
    assert readiness._predicted_yes_probability(row) is None


@pytest.mark.parametrize("bad_proba", [-0.01, 1.01, 1.5, -1.0, 2.0])
def test_predicted_yes_probability_rejects_out_of_range(bad_proba: float) -> None:
    # Codex pattern-6 catch: finite but out-of-[0,1] values must not pollute
    # bucket averages. A misbehaving model emitting 1.05 silently dropped to
    # ``settled_count=0`` for that row pre-fix; post-fix it's explicit.
    row = _Row(fair_yes_price=bad_proba)
    assert readiness._predicted_yes_probability(row) is None


def test_calibration_buckets_skips_out_of_range_probabilities() -> None:
    rows = [
        _row(p=1.5, outcome="won"),    # out of range → skipped
        _row(p=-0.1, outcome="won"),   # out of range → skipped
        _row(p=0.5, outcome="won"),    # in range → counted
    ]
    target = next(b for b in readiness._calibration_buckets(rows) if b["label"] == "50-60%")
    assert target["settled_count"] == 1


# -- _calibration_buckets ------------------------------------------------------


def _row(*, p: float, side: str = "yes", outcome: str = "won") -> _Row:
    return _Row(fair_yes_price=p, side=side, prediction_outcome=outcome)


def test_calibration_buckets_empty_input_returns_one_dict_per_bucket() -> None:
    result = readiness._calibration_buckets([])
    assert len(result) == 10
    assert {b["label"] for b in result} == {
        "0-10%",
        "10-20%",
        "20-30%",
        "30-40%",
        "40-50%",
        "50-60%",
        "60-70%",
        "70-80%",
        "80-90%",
        "90-100%",
    }
    for bucket in result:
        assert bucket["settled_count"] == 0
        assert bucket["avg_predicted"] is None
        assert bucket["actual_yes_rate"] is None
        assert bucket["miscalibration"] is None


def test_calibration_buckets_perfect_calibration_yields_zero_miscalibration() -> None:
    # 10 rows in the 60-70% bucket, all predicted 0.65, exactly 6.5 of 10 yes-happens
    # is impossible with integer counts — use 0.6 predicted and 6/10 yes-rate.
    rows = [_row(p=0.60, outcome="won") for _ in range(6)] + [
        _row(p=0.60, outcome="lost") for _ in range(4)
    ]
    buckets = {b["label"]: b for b in readiness._calibration_buckets(rows)}
    target = buckets["60-70%"]
    assert target["settled_count"] == 10
    assert target["avg_predicted"] == pytest.approx(0.60)
    assert target["actual_yes_rate"] == pytest.approx(0.60)
    assert target["miscalibration"] == pytest.approx(0.0)


def test_calibration_buckets_over_confidence_yields_positive_miscalibration() -> None:
    # Predicted 0.80 on average; only 5/10 YES outcomes → over-confident by 0.30.
    rows = [_row(p=0.80, outcome="won") for _ in range(5)] + [
        _row(p=0.80, outcome="lost") for _ in range(5)
    ]
    target = next(b for b in readiness._calibration_buckets(rows) if b["label"] == "80-90%")
    assert target["miscalibration"] == pytest.approx(0.30)


def test_calibration_buckets_under_confidence_yields_negative_miscalibration() -> None:
    # Predicted 0.40 on average; 7/10 YES outcomes → under-confident by 0.30.
    rows = [_row(p=0.40, outcome="won") for _ in range(7)] + [
        _row(p=0.40, outcome="lost") for _ in range(3)
    ]
    target = next(b for b in readiness._calibration_buckets(rows) if b["label"] == "40-50%")
    assert target["miscalibration"] == pytest.approx(-0.30)


def test_calibration_buckets_no_side_outcomes_invert_correctly() -> None:
    # NO-side picks: ``won`` means YES did NOT happen. Predicted 0.30 P(YES);
    # 7/10 picks won → 7/10 had YES=False → actual_yes_rate = 3/10.
    rows = [_row(p=0.30, side="no", outcome="won") for _ in range(7)] + [
        _row(p=0.30, side="no", outcome="lost") for _ in range(3)
    ]
    target = next(b for b in readiness._calibration_buckets(rows) if b["label"] == "30-40%")
    assert target["settled_count"] == 10
    assert target["avg_predicted"] == pytest.approx(0.30)
    assert target["actual_yes_rate"] == pytest.approx(0.30)
    assert target["miscalibration"] == pytest.approx(0.0)


def test_calibration_buckets_skips_push_and_cancelled() -> None:
    rows = [
        _row(p=0.50, outcome="won"),
        _row(p=0.50, outcome="push"),
        _row(p=0.50, outcome="cancelled"),
        _row(p=0.50, outcome="lost"),
    ]
    target = next(b for b in readiness._calibration_buckets(rows) if b["label"] == "50-60%")
    # Only the won + lost rows count.
    assert target["settled_count"] == 2


def test_calibration_buckets_skips_pending_and_unparseable() -> None:
    rows = [
        _row(p=0.50, outcome="pending"),
        _row(p=0.50, outcome="won"),
        _Row(fair_yes_price=None, side="yes", prediction_outcome="won"),  # no proba
        _Row(fair_yes_price=float("inf"), side="yes", prediction_outcome="won"),  # non-finite
    ]
    target = next(b for b in readiness._calibration_buckets(rows) if b["label"] == "50-60%")
    assert target["settled_count"] == 1


def test_calibration_buckets_boundary_lower_inclusive_upper_exclusive() -> None:
    # 0.60 belongs to 60-70%, not 50-60%.
    rows = [_row(p=0.60, outcome="won")]
    counts = {b["label"]: b["settled_count"] for b in readiness._calibration_buckets(rows)}
    assert counts["50-60%"] == 0
    assert counts["60-70%"] == 1


def test_calibration_buckets_perfect_yes_lands_in_top_bucket() -> None:
    # 1.0 must land somewhere — the last bucket is inclusive at its upper edge.
    rows = [_row(p=1.0, outcome="won")]
    counts = {b["label"]: b["settled_count"] for b in readiness._calibration_buckets(rows)}
    assert counts["90-100%"] == 1


def test_calibration_buckets_parlay_rows_use_combined_probability() -> None:
    # Parlay rows have ``combined_model_probability`` set (and no
    # ``fair_yes_price``). The helper must still bucket them.
    rows = [
        _Row(combined_model_probability=0.20, side="yes", prediction_outcome="won")
        for _ in range(3)
    ] + [
        _Row(combined_model_probability=0.20, side="yes", prediction_outcome="lost")
        for _ in range(7)
    ]
    target = next(b for b in readiness._calibration_buckets(rows) if b["label"] == "20-30%")
    assert target["settled_count"] == 10
    assert target["avg_predicted"] == pytest.approx(0.20)
    assert target["actual_yes_rate"] == pytest.approx(0.30)
    # Under-confidence: model said 0.20 P(YES), reality came in at 0.30.
    assert target["miscalibration"] == pytest.approx(-0.10)


def test_did_yes_happen_parlay_row_without_side_treats_won_as_yes() -> None:
    # Codex pattern-9 catch: ``ParlayPrediction`` has no ``side`` column.
    # ``combined_model_probability`` already encodes the joint probability;
    # outcome "won" matches the YES axis directly without inversion.
    assert readiness._did_yes_happen(_ParlayRow(prediction_outcome="won")) is True
    assert readiness._did_yes_happen(_ParlayRow(prediction_outcome="lost")) is False


def test_calibration_buckets_real_parlay_shape_without_side_attribute() -> None:
    # Verifies the fix for the codex-pattern-9 catch: a parlay row with NO
    # ``side`` attribute at all (not even a default) must still contribute
    # to calibration buckets. Pre-fix this returned 0 settled rows because
    # ``_did_yes_happen`` short-circuited on the missing/empty side.
    rows = [_ParlayRow(combined_model_probability=0.30, prediction_outcome="won") for _ in range(4)] + [
        _ParlayRow(combined_model_probability=0.30, prediction_outcome="lost") for _ in range(6)
    ]
    target = next(b for b in readiness._calibration_buckets(rows) if b["label"] == "30-40%")
    assert target["settled_count"] == 10
    assert target["avg_predicted"] == pytest.approx(0.30)
    assert target["actual_yes_rate"] == pytest.approx(0.40)
    assert target["miscalibration"] == pytest.approx(-0.10)


def test_calibration_buckets_single_row_per_bucket() -> None:
    # One row per bucket — actual_yes_rate is either 0 or 1; the curve is jagged.
    rows = [
        _row(p=0.05, outcome="won"),
        _row(p=0.15, outcome="lost"),
        _row(p=0.95, outcome="won"),
    ]
    buckets = {b["label"]: b for b in readiness._calibration_buckets(rows)}
    assert buckets["0-10%"]["actual_yes_rate"] == pytest.approx(1.0)
    assert buckets["10-20%"]["actual_yes_rate"] == pytest.approx(0.0)
    assert buckets["90-100%"]["actual_yes_rate"] == pytest.approx(1.0)
