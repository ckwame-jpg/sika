"""Tests for Smarter #2 phase 2 — DB query layer + walk-forward
orchestration on top of the phase 1 math.

Covers:

- ``query_family_walk_forward_inputs`` filters by family, date
  window, and outcome whitelist
- YES/NO side mapping for singles
- Parlay rows take ``combined_model_probability`` directly
- Malformed probabilities (NaN, out of range) get dropped
- ``compute_family_walk_forward`` end-to-end produces folds
- Drift guard: the api-side math agrees with apps/ml's canonical
  implementation on a shared synthetic input
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Event,
    Market,
    ParlayPrediction,
    Prediction,
    Run,
)
from app.services.ml.walk_forward import (
    DEFAULT_LOOKBACK_DAYS,
    WalkForwardFold,
    _outcome_for_parlay,
    _outcome_for_single,
    _safe_probability,
    compute_family_walk_forward,
    expected_calibration_error,
    query_family_walk_forward_inputs,
    walk_forward_evaluate,
)

# Anchor for all date math. ``compute_family_walk_forward`` uses
# this as the explicit ``end_date`` so the lookback window is
# deterministic regardless of when the test runs.
_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


# -- Seed helpers ------------------------------------------------------


def _seed_market(db_session, *, ticker: str, sport_key: str, market_family: str | None) -> Market:
    market = Market(
        ticker=ticker,
        sport_key=sport_key,
        title=f"{ticker} title",
        status="open",
        raw_data={"copilot_market_family": market_family} if market_family else {},
    )
    db_session.add(market)
    db_session.flush()
    return market


def _seed_prediction(
    db_session,
    *,
    market: Market,
    sport_key: str,
    market_family: str | None,
    fair_yes: float,
    side: str,
    outcome: str,
    captured_at: datetime,
) -> Prediction:
    pred = Prediction(
        market_id=market.id,
        ticker=market.ticker,
        sport_key=sport_key,
        market_title=market.title,
        market_family=market_family,
        side=side,
        action="buy",
        suggested_price=0.50,
        fair_yes_price=fair_yes,
        edge=0.05,
        confidence=0.60,
        rationale="test",
        prediction_outcome=outcome,
        settlement_status="settled" if outcome in ("won", "lost") else "pending",
        captured_at=captured_at,
    )
    db_session.add(pred)
    db_session.flush()
    return pred


def _seed_parlay_prediction(
    db_session,
    *,
    leg_count: int,
    sport_scope: str,
    combined_prob: float,
    outcome: str,
    captured_at: datetime,
) -> ParlayPrediction:
    pred = ParlayPrediction(
        leg_count=leg_count,
        sport_scope=sport_scope,
        participating_sports=[sport_scope],
        combined_market_price=0.40,
        combined_model_probability=combined_prob,
        american_odds="+150",
        edge=0.05,
        confidence=0.60,
        invalidation="test",
        rationale="test",
        prediction_outcome=outcome,
        settlement_status="settled" if outcome in ("won", "lost") else "pending",
        captured_at=captured_at,
    )
    db_session.add(pred)
    db_session.flush()
    return pred


# -- Outcome / probability helpers --------------------------------------


def test_outcome_for_single_yes_won() -> None:
    assert _outcome_for_single("yes", "won") == 1


def test_outcome_for_single_yes_lost() -> None:
    assert _outcome_for_single("yes", "lost") == 0


def test_outcome_for_single_no_won_inverts_yes_axis() -> None:
    """NO side that won → from the YES axis the outcome is 0
    (model picked NO, NO happened, so YES did NOT happen)."""
    assert _outcome_for_single("no", "won") == 0


def test_outcome_for_single_no_lost_inverts_yes_axis() -> None:
    assert _outcome_for_single("no", "lost") == 1


def test_outcome_for_single_skips_undecided() -> None:
    assert _outcome_for_single("yes", "push") is None
    assert _outcome_for_single("yes", "pending") is None
    assert _outcome_for_single("yes", "cancelled") is None


def test_outcome_for_single_skips_unknown_side() -> None:
    assert _outcome_for_single("either", "won") is None
    assert _outcome_for_single(None, "won") is None


def test_outcome_for_parlay_won_is_yes_axis() -> None:
    """Parlays have no per-row side — combined_model_probability is
    already the joint probability, so 'won' = YES axis directly."""
    assert _outcome_for_parlay("won") == 1
    assert _outcome_for_parlay("lost") == 0


def test_outcome_for_parlay_skips_undecided() -> None:
    assert _outcome_for_parlay("push") is None
    assert _outcome_for_parlay("pending") is None


def test_safe_probability_accepts_in_range() -> None:
    assert _safe_probability(0.5) == 0.5
    assert _safe_probability(0.0) == 0.0
    assert _safe_probability(1.0) == 1.0


def test_safe_probability_rejects_nan_inf() -> None:
    assert _safe_probability(float("nan")) is None
    assert _safe_probability(float("inf")) is None
    assert _safe_probability(float("-inf")) is None


def test_safe_probability_rejects_out_of_range() -> None:
    assert _safe_probability(-0.1) is None
    assert _safe_probability(1.5) is None


def test_safe_probability_rejects_unparseable() -> None:
    assert _safe_probability(None) is None
    assert _safe_probability("not a number") is None
    assert _safe_probability(object()) is None


# -- Family predicate filtering ----------------------------------------


def test_query_filters_by_sport_for_nba_singles(db_session) -> None:
    nba_market = _seed_market(db_session, ticker="NBA-1", sport_key="NBA", market_family=None)
    mlb_market = _seed_market(db_session, ticker="MLB-1", sport_key="MLB", market_family=None)
    _seed_prediction(
        db_session, market=nba_market, sport_key="NBA", market_family=None,
        fair_yes=0.55, side="yes", outcome="won", captured_at=_NOW - timedelta(days=10),
    )
    _seed_prediction(
        db_session, market=mlb_market, sport_key="MLB", market_family=None,
        fair_yes=0.55, side="yes", outcome="won", captured_at=_NOW - timedelta(days=10),
    )

    rows = query_family_walk_forward_inputs(
        db_session, "nba_singles", end_date=_NOW,
    )

    assert len(rows) == 1


def test_query_filters_player_props_separately_from_singles(db_session) -> None:
    """nba_props != nba_singles — player_prop rows belong to the
    props family, everything else to singles."""
    market = _seed_market(db_session, ticker="NBA-1", sport_key="NBA", market_family="player_prop")
    other_market = _seed_market(db_session, ticker="NBA-2", sport_key="NBA", market_family="winner")
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family="player_prop",
        fair_yes=0.55, side="yes", outcome="won", captured_at=_NOW - timedelta(days=5),
    )
    _seed_prediction(
        db_session, market=other_market, sport_key="NBA", market_family="winner",
        fair_yes=0.45, side="no", outcome="lost", captured_at=_NOW - timedelta(days=5),
    )

    props = query_family_walk_forward_inputs(db_session, "nba_props", end_date=_NOW)
    singles = query_family_walk_forward_inputs(db_session, "nba_singles", end_date=_NOW)

    assert len(props) == 1
    assert len(singles) == 1
    # Each family sees only its own row, not the other.
    assert props[0][1] == 0.55
    assert singles[0][1] == 0.45


def test_query_excludes_unsettled_rows(db_session) -> None:
    market = _seed_market(db_session, ticker="NBA-1", sport_key="NBA", market_family=None)
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=0.55, side="yes", outcome="won", captured_at=_NOW - timedelta(days=10),
    )
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=0.55, side="yes", outcome="pending", captured_at=_NOW - timedelta(days=10),
    )
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=0.55, side="yes", outcome="push", captured_at=_NOW - timedelta(days=10),
    )

    rows = query_family_walk_forward_inputs(db_session, "nba_singles", end_date=_NOW)

    assert len(rows) == 1


def test_query_respects_lookback_window(db_session) -> None:
    market = _seed_market(db_session, ticker="NBA-1", sport_key="NBA", market_family=None)
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=0.55, side="yes", outcome="won",
        captured_at=_NOW - timedelta(days=5),  # inside default 180d window
    )
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=0.55, side="yes", outcome="won",
        captured_at=_NOW - timedelta(days=200),  # outside default 180d
    )

    rows = query_family_walk_forward_inputs(db_session, "nba_singles", end_date=_NOW)

    assert len(rows) == 1
    assert rows[0][0] >= _NOW - timedelta(days=DEFAULT_LOOKBACK_DAYS)


def test_query_respects_custom_lookback(db_session) -> None:
    market = _seed_market(db_session, ticker="NBA-1", sport_key="NBA", market_family=None)
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=0.55, side="yes", outcome="won",
        captured_at=_NOW - timedelta(days=10),
    )
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=0.55, side="yes", outcome="won",
        captured_at=_NOW - timedelta(days=30),
    )

    short = query_family_walk_forward_inputs(
        db_session, "nba_singles", end_date=_NOW, lookback_days=20,
    )
    long = query_family_walk_forward_inputs(
        db_session, "nba_singles", end_date=_NOW, lookback_days=60,
    )

    assert len(short) == 1
    assert len(long) == 2


def test_query_skips_malformed_probability_rows(db_session) -> None:
    """A row with fair_yes outside [0, 1] (legacy data, manual SQL,
    etc.) gets dropped — it would poison the walk-forward Brier."""
    market = _seed_market(db_session, ticker="NBA-1", sport_key="NBA", market_family=None)
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=0.55, side="yes", outcome="won", captured_at=_NOW - timedelta(days=10),
    )
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=1.5, side="yes", outcome="won", captured_at=_NOW - timedelta(days=10),
    )
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=None, side="yes", outcome="won", captured_at=_NOW - timedelta(days=10),
    )

    rows = query_family_walk_forward_inputs(db_session, "nba_singles", end_date=_NOW)

    assert len(rows) == 1
    assert rows[0][1] == 0.55


def test_query_handles_no_side_inversion(db_session) -> None:
    """A NO-side prediction that won maps to YES outcome 0 (YES did
    NOT happen). Verifies the side-aware mapping is applied at
    query time rather than treating outcome literally."""
    market = _seed_market(db_session, ticker="NBA-1", sport_key="NBA", market_family=None)
    _seed_prediction(
        db_session, market=market, sport_key="NBA", market_family=None,
        fair_yes=0.40, side="no", outcome="won", captured_at=_NOW - timedelta(days=10),
    )

    rows = query_family_walk_forward_inputs(db_session, "nba_singles", end_date=_NOW)

    assert len(rows) == 1
    assert rows[0][2] == 0  # YES did not happen


def test_query_returns_empty_for_unknown_family(db_session) -> None:
    """Operator endpoints that pass a typo'd family key get an
    empty fold series (no full-table scan, no exception)."""
    rows = query_family_walk_forward_inputs(
        db_session, "nba_singlez_typo", end_date=_NOW,
    )
    assert rows == []


def test_query_rejects_non_positive_lookback(db_session) -> None:
    with pytest.raises(ValueError, match="lookback_days"):
        query_family_walk_forward_inputs(
            db_session, "nba_singles", end_date=_NOW, lookback_days=0,
        )


# -- Parlay rows --------------------------------------------------------


def test_query_parlay_family_takes_combined_probability(db_session) -> None:
    _seed_parlay_prediction(
        db_session, leg_count=2, sport_scope="NBA",
        combined_prob=0.30, outcome="won",
        captured_at=_NOW - timedelta(days=10),
    )
    _seed_parlay_prediction(
        db_session, leg_count=3, sport_scope="NBA",
        combined_prob=0.15, outcome="lost",
        captured_at=_NOW - timedelta(days=10),
    )

    rows_2leg = query_family_walk_forward_inputs(
        db_session, "nba_parlay_2leg", end_date=_NOW,
    )
    rows_3leg = query_family_walk_forward_inputs(
        db_session, "nba_parlay_3leg", end_date=_NOW,
    )

    assert len(rows_2leg) == 1 and rows_2leg[0][1] == pytest.approx(0.30)
    assert len(rows_3leg) == 1 and rows_3leg[0][1] == pytest.approx(0.15)


def test_query_parlay_4_6_leg_combiner_matches_band(db_session) -> None:
    """Combiner family matches leg_count in [4, 6]."""
    _seed_parlay_prediction(
        db_session, leg_count=4, sport_scope="MIXED",
        combined_prob=0.05, outcome="lost", captured_at=_NOW - timedelta(days=5),
    )
    _seed_parlay_prediction(
        db_session, leg_count=7, sport_scope="MIXED",  # outside band
        combined_prob=0.02, outcome="lost", captured_at=_NOW - timedelta(days=5),
    )

    rows = query_family_walk_forward_inputs(
        db_session, "parlay_4_6_leg_combiner", end_date=_NOW,
    )

    assert len(rows) == 1
    assert rows[0][1] == pytest.approx(0.05)


# -- compute_family_walk_forward ---------------------------------------


def test_compute_returns_empty_when_no_inputs(db_session) -> None:
    folds = compute_family_walk_forward(db_session, "nba_singles", end_date=_NOW)
    assert folds == []


def test_compute_returns_folds_for_seeded_history(db_session) -> None:
    """End-to-end: seed enough rows over multiple weeks to clear
    min_per_fold, then verify the orchestration returns folds."""
    market = _seed_market(db_session, ticker="NBA-1", sport_key="NBA", market_family=None)
    # 60 settled predictions across ~30 days → at fold_days=14 we
    # get at least 2 folds with min_per_fold=10.
    for offset in range(60):
        _seed_prediction(
            db_session, market=market, sport_key="NBA", market_family=None,
            fair_yes=0.55, side="yes",
            outcome="won" if offset % 2 == 0 else "lost",
            captured_at=_NOW - timedelta(days=30) + timedelta(hours=offset * 12),
        )

    folds = compute_family_walk_forward(
        db_session, "nba_singles", end_date=_NOW,
        lookback_days=45, fold_days=14, min_per_fold=10,
    )

    assert len(folds) >= 2
    assert all(isinstance(f, WalkForwardFold) for f in folds)
    # Folds tile contiguously in time order.
    starts = [f.start for f in folds]
    assert starts == sorted(starts)


# -- Drift guard ------------------------------------------------------


def test_walk_forward_evaluate_matches_apps_ml_canonical() -> None:
    """Bug #29 pattern: api-side ``walk_forward_evaluate`` is a
    mirror of ``apps/ml/ml/backtest.walk_forward_evaluate``. Drift
    between them silently breaks the promotion gate when phase 3
    swaps in the api-side metric. Pin agreement on a shared
    synthetic input.

    Skips when apps/ml isn't on sys.path (CI / packaging contexts).
    """
    pytest.importorskip("ml.backtest")
    from ml.backtest import walk_forward_evaluate as canonical_evaluate

    timestamps = [
        datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(hours=i)
        for i in range(120)
    ]
    probs = [0.4 + (i % 7) * 0.05 for i in range(120)]
    outcomes = [(i + (i % 3)) % 2 for i in range(120)]

    api_folds = walk_forward_evaluate(
        timestamps, probs, outcomes, fold_days=2, min_per_fold=15,
    )
    canonical_folds = canonical_evaluate(
        timestamps, probs, outcomes, fold_days=2, min_per_fold=15,
    )

    assert len(api_folds) == len(canonical_folds)
    for a, c in zip(api_folds, canonical_folds):
        assert a.start == c.start
        assert a.end == c.end
        assert a.sample_size == c.sample_size
        assert a.brier == pytest.approx(c.brier, abs=1e-12)
        assert a.ece == pytest.approx(c.ece, abs=1e-12)
        assert a.log_loss == pytest.approx(c.log_loss, abs=1e-12)


def test_expected_calibration_error_matches_recalibration_canonical() -> None:
    """Same drift guard for the ECE metric (which the math layer
    re-implements rather than imports from
    ``apps/ml/ml/recalibration.py``)."""
    pytest.importorskip("ml.recalibration")
    from ml.recalibration import expected_calibration_error as canonical_ece

    import numpy as np

    rng = np.random.default_rng(20260516)
    probs = rng.uniform(0.0, 1.0, size=200).tolist()
    outcomes = (rng.uniform(0.0, 1.0, size=200) < np.asarray(probs)).astype(int).tolist()

    api_value = expected_calibration_error(probs, outcomes, n_bins=10)
    canonical_value = canonical_ece(
        np.asarray(probs), np.asarray(outcomes), n_bins=10,
    )

    assert api_value == pytest.approx(canonical_value, abs=1e-12)
