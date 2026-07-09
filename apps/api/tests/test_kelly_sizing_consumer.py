"""Tests for Smarter #9 phase 3 — consumer helper composing the
phase 1 math + phase 2 DB inputs, plus the persistence-layer
enrichment that lands the sizing block on every scored
recommendation.

Covers:
- ``compute_kelly_sizing_diagnostics`` returns the expected dict
  with default settings (test fixture defaults match the env
  defaults via ``Settings()``).
- Returns ``None`` when bankroll resolution fails / probability
  is invalid / price is degenerate.
- NO side correctly inverts probability + price for the Kelly
  axis.
- ``_enrich_with_kelly_sizing`` (persistence-layer hook) writes
  the ``kelly_sizing`` block onto both signal and recommendation
  diagnostics, only for ``capture_scope="recommendation"``, and
  is robust to suppressed recommendations.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models import Market, Recommendation, SignalSnapshot
from app.services.kelly_sizing_consumer import compute_kelly_sizing_diagnostics
from app.services.scoring.persistence import _enrich_with_kelly_sizing
from app.services.scoring.types import ScoredRecommendation, ScoredWatchlistCapture


# -- compute_kelly_sizing_diagnostics ---------------------------------


def test_returns_expected_block_for_yes_side(db_session) -> None:
    """Defaults: bankroll = $1000, no rolling PnL = no brake. A
    YES pick at probability=0.55 / price=0.50 has edge 0.05, raw
    Kelly = 0.05/0.50 = 0.10, fractional = 0.025. With default cap
    0.02 the clamped fraction lands at 0.02 ($20)."""
    out = compute_kelly_sizing_diagnostics(
        db_session,
        probability_yes=0.55,
        price_yes=0.50,
        side="yes",
    )
    assert out is not None
    assert out["fraction"] == pytest.approx(0.02)
    assert out["dollars"] == pytest.approx(20.00)
    assert out["raw_kelly"] == pytest.approx(0.10)
    assert out["fractional_kelly"] == pytest.approx(0.025)
    assert out["brake_multiplier"] == 1.0
    assert out["below_floor"] is False
    assert out["bankroll"] == 1000.0


def test_no_side_inverts_probability_and_price(db_session) -> None:
    """A NO pick at YES probability=0.45 / YES price=0.60 maps to
    selected probability = 0.55 / selected price = 0.40. Selected
    edge = 0.15, raw Kelly = 0.15/0.60 = 0.25, fractional = 0.0625,
    clamped to 0.02."""
    out = compute_kelly_sizing_diagnostics(
        db_session,
        probability_yes=0.45,
        price_yes=0.60,
        side="no",
    )
    assert out is not None
    assert out["raw_kelly"] == pytest.approx(0.25)
    assert out["fractional_kelly"] == pytest.approx(0.0625)
    assert out["fraction"] == pytest.approx(0.02)  # cap


def test_returns_none_when_probability_invalid(db_session) -> None:
    assert compute_kelly_sizing_diagnostics(
        db_session, probability_yes=-0.1, price_yes=0.5, side="yes",
    ) is None
    assert compute_kelly_sizing_diagnostics(
        db_session, probability_yes=1.5, price_yes=0.5, side="yes",
    ) is None
    assert compute_kelly_sizing_diagnostics(
        db_session, probability_yes=float("nan"), price_yes=0.5, side="yes",
    ) is None


def test_returns_none_when_price_invalid(db_session) -> None:
    assert compute_kelly_sizing_diagnostics(
        db_session, probability_yes=0.5, price_yes=0.0, side="yes",
    ) is None
    assert compute_kelly_sizing_diagnostics(
        db_session, probability_yes=0.5, price_yes=1.0, side="yes",
    ) is None


def test_returns_none_when_no_side_produces_degenerate_price(db_session) -> None:
    """YES price = 1.0 inverts to NO price 0.0 (degenerate)."""
    assert compute_kelly_sizing_diagnostics(
        db_session,
        probability_yes=0.5,
        price_yes=0.999,
        side="no",
    )["fraction"] >= 0.0  # 1-0.999 = 0.001 is technically valid
    # But price_yes=1.0 → NO price 0.0 → None
    assert compute_kelly_sizing_diagnostics(
        db_session,
        probability_yes=0.5,
        price_yes=1.0,
        side="no",
    ) is None


def test_returns_none_for_unknown_side(db_session) -> None:
    assert compute_kelly_sizing_diagnostics(
        db_session, probability_yes=0.55, price_yes=0.5, side="either",
    ) is None


def test_no_edge_yields_zero_fraction(db_session) -> None:
    """probability_yes == price_yes → Kelly says don't bet → fraction 0
    + below_floor=True."""
    out = compute_kelly_sizing_diagnostics(
        db_session, probability_yes=0.50, price_yes=0.50, side="yes",
    )
    assert out is not None
    assert out["fraction"] == 0.0
    assert out["below_floor"] is True


# -- _enrich_with_kelly_sizing ---------------------------------------


def _seed_market(db_session) -> Market:
    market = Market(
        ticker="NBA-T", sport_key="NBA", title="t", status="open", raw_data={},
    )
    db_session.add(market)
    db_session.flush()
    return market


def _make_capture(
    db_session,
    *,
    probability_yes: float = 0.55,
    price: float = 0.50,
    side: str = "yes",
    capture_scope: str | None = "recommendation",
    recommendation_present: bool = True,
) -> ScoredWatchlistCapture:
    market = _seed_market(db_session)
    signal = SignalSnapshot(
        market_id=market.id,
        fair_yes_price=probability_yes,
        fair_no_price=1.0 - probability_yes,
        confidence=0.7,
        edge=0.05,
        reasons=[],
        features={},
        scoring_diagnostics={},
    )
    recommendation = None
    if recommendation_present:
        recommendation = Recommendation(
            event_id=None,
            market_id=market.id,
            side=side,
            action="buy",
            status="active",
            suggested_price=price,
            edge=0.05,
            confidence=0.7,
            invalidation="x",
            rationale="x",
            scoring_diagnostics={},
        )
    return ScoredWatchlistCapture(
        market=market,
        scored=ScoredRecommendation(
            recommendation=recommendation,
            signal=signal,
            metadata={},
        ),
        capture_scope=capture_scope,
    )


def test_enrich_writes_kelly_block_on_both_diagnostics(db_session) -> None:
    capture = _make_capture(db_session)
    _enrich_with_kelly_sizing(db_session, capture)
    signal_diag = capture.scored.signal.scoring_diagnostics or {}
    rec_diag = capture.scored.recommendation.scoring_diagnostics or {}
    assert "kelly_sizing" in signal_diag
    assert "kelly_sizing" in rec_diag
    # Both copies are identical (same dict reference is acceptable).
    assert signal_diag["kelly_sizing"]["fraction"] == rec_diag["kelly_sizing"]["fraction"]


def test_enrich_no_side_uses_no_entry_price_not_its_complement(db_session) -> None:
    # NO pick: model P(YES)=0.30 (P(NO)=0.70) and NO ask = 0.60 (the
    # side-relative suggested_price). Correct raw Kelly = (0.70 - 0.60) /
    # (1 - 0.60) = 0.25. The pre-fix hook passed 0.60 as price_yes and the
    # consumer re-inverted it to 0.40, producing a phantom raw Kelly of 0.50.
    capture = _make_capture(db_session, probability_yes=0.30, price=0.60, side="no")
    _enrich_with_kelly_sizing(db_session, capture)
    block = capture.scored.recommendation.scoring_diagnostics["kelly_sizing"]
    assert block["raw_kelly"] == pytest.approx(0.25)


def test_enrich_no_side_positive_edge_longshot_not_suppressed(db_session) -> None:
    # NO longshot: P(YES)=0.78 (P(NO)=0.22), NO ask=0.17 → real positive edge.
    # Correct fractional Kelly ~= 0.015 (above the 0.005 floor). The pre-fix
    # double inversion made P(NO)=0.22 <= price 0.83, so raw Kelly was 0 and
    # the position was wrongly suppressed as below_floor.
    capture = _make_capture(db_session, probability_yes=0.78, price=0.17, side="no")
    _enrich_with_kelly_sizing(db_session, capture)
    block = capture.scored.recommendation.scoring_diagnostics["kelly_sizing"]
    assert block["below_floor"] is False
    # Persisted raw_kelly is rounded to 4 decimals (~0.0602).
    assert block["raw_kelly"] == pytest.approx((0.22 - 0.17) / (1 - 0.17), abs=1e-4)
    assert block["fraction"] > 0.0


def test_enrich_skips_when_capture_scope_not_recommendation(db_session) -> None:
    """Coverage-only or signal-only captures don't get sized."""
    capture = _make_capture(db_session, capture_scope="coverage")
    _enrich_with_kelly_sizing(db_session, capture)
    assert "kelly_sizing" not in (capture.scored.signal.scoring_diagnostics or {})


def test_enrich_skips_when_recommendation_is_none(db_session) -> None:
    """Suppressed-by-monotonicity captures keep capture_scope set but
    nullify the recommendation. Enrichment must skip — no side to
    pick from."""
    capture = _make_capture(db_session, recommendation_present=False)
    _enrich_with_kelly_sizing(db_session, capture)
    assert "kelly_sizing" not in (capture.scored.signal.scoring_diagnostics or {})


def test_enrich_swallows_helper_exception(db_session, monkeypatch) -> None:
    """If the helper raises, persistence must continue uncrashed."""
    from app.services.scoring import persistence as persistence_module

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    # Patch the import target inside ``_enrich_with_kelly_sizing``.
    monkeypatch.setattr(
        "app.services.kelly_sizing_consumer.compute_kelly_sizing_diagnostics",
        boom,
    )
    capture = _make_capture(db_session)
    # Should not raise.
    _enrich_with_kelly_sizing(db_session, capture)
    # No sizing block landed.
    assert "kelly_sizing" not in (capture.scored.signal.scoring_diagnostics or {})


def test_enrich_preserves_existing_diagnostics(db_session) -> None:
    """The merge must extend, not replace, existing diagnostic
    fields."""
    capture = _make_capture(db_session)
    capture.scored.signal.scoring_diagnostics = {"existing_field": "value"}
    capture.scored.recommendation.scoring_diagnostics = {"existing_field": "value"}
    _enrich_with_kelly_sizing(db_session, capture)
    signal_diag = capture.scored.signal.scoring_diagnostics
    assert signal_diag.get("existing_field") == "value"
    assert "kelly_sizing" in signal_diag
