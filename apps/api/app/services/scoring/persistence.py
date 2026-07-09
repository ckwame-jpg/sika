"""Scored-recommendation persistence + Prediction→Recommendation
rehydration helpers.

Extracted from ``scoring/__init__.py`` as part of R1. These four
helpers were tightly coupled to the scoring kernel but had no
dependency back on the scoring math — they read settled
``Prediction`` rows and produce ``Recommendation`` / ``SignalSnapshot``
/ ``ParlayCandidateInput`` instances, and the ``_persist_*`` helper
flushes scored captures into the DB. Keeping them together here
keeps the scoring kernel focused on the scoring decision rather
than the DB-write tail.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import (
    Market,
    Prediction,
    Recommendation,
    SignalSnapshot,
)
from app.services.parlays import ParlayCandidateInput
from app.services.predictions import MODEL_NAME, capture_prediction
from app.services.scoring.types import ScoredWatchlistCapture

__all__ = [
    "_build_recommendation_from_prediction",
    "_signal_snapshot_from_prediction",
    "_parlay_candidate_from_prediction",
    "_persist_scored_watchlist_captures",
]


def _build_recommendation_from_prediction(prediction: Prediction) -> Recommendation:
    return Recommendation(
        event_id=prediction.event_id,
        market_id=prediction.market_id,
        side=prediction.side,
        action=prediction.action,
        status="active",
        suggested_price=prediction.suggested_price,
        edge=prediction.edge,
        confidence=prediction.confidence,
        selection_score=prediction.selection_score,
        model_name=prediction.model_name,
        model_version=prediction.model_version,
        calibration_version=prediction.calibration_version,
        feature_set_version=prediction.feature_set_version,
        model_metadata=dict(prediction.model_metadata or {}),
        invalidation=prediction.invalidation or "Pull if execution conditions materially change.",
        rationale=prediction.rationale,
        scoring_diagnostics=dict(prediction.scoring_diagnostics or {}),
        captured_at=prediction.captured_at,
    )


def _signal_snapshot_from_prediction(prediction: Prediction) -> SignalSnapshot:
    fair_yes_price = float(prediction.fair_yes_price or 0.0)
    fair_no_price = float(prediction.fair_no_price if prediction.fair_no_price is not None else (1 - fair_yes_price))
    return SignalSnapshot(
        event_id=prediction.event_id,
        market_id=prediction.market_id,
        captured_at=prediction.captured_at,
        model_name=prediction.model_name or MODEL_NAME,
        model_version=prediction.model_version,
        calibration_version=prediction.calibration_version,
        feature_set_version=prediction.feature_set_version,
        model_metadata=dict(prediction.model_metadata or {}),
        confidence=prediction.confidence,
        fair_yes_price=fair_yes_price,
        fair_no_price=fair_no_price,
        edge=prediction.edge,
        selection_score=prediction.selection_score,
        reasons=list(prediction.reasons or []),
        features=dict(prediction.features or {}),
        scoring_diagnostics=dict(prediction.scoring_diagnostics or {}),
    )


def _parlay_candidate_from_prediction(prediction: Prediction) -> ParlayCandidateInput | None:
    market = prediction.market
    event = market.event if market is not None else None
    if market is None or event is None:
        return None
    return ParlayCandidateInput(
        event=event,
        market=market,
        recommendation=_build_recommendation_from_prediction(prediction),
        signal=_signal_snapshot_from_prediction(prediction),
        prediction=prediction,
        metadata=dict(market.raw_data or {}),
    )


def _enrich_with_kelly_sizing(
    db: Session, capture: ScoredWatchlistCapture,
) -> None:
    """Smarter #9 phase 3: compute the suggested position size for a
    scored recommendation and merge the diagnostic block into both
    the signal and the recommendation's ``scoring_diagnostics`` JSON.

    ``capture.scored.recommendation`` carries the picked side +
    suggested_price; ``capture.scored.signal`` carries the model's
    P(YES). The helper consumes both, handles the side-aware mapping
    onto the Kelly axis, and stashes the resulting size + provenance
    under the ``kelly_sizing`` key. Returns silently when:

    - Capture scope is not ``"recommendation"`` (no need to size a
      coverage-only signal).
    - ``recommendation`` is None (suppressed by monotonicity etc.).
    - The sizing helper returns None (no bankroll, invalid inputs).

    Wrapped in try/except so a sizing failure never crashes the
    persistence path — sizing is observability, not correctness.
    """
    if capture.capture_scope != "recommendation":
        return
    recommendation = capture.scored.recommendation
    signal = capture.scored.signal
    if recommendation is None or signal is None:
        return
    side = str(getattr(recommendation, "side", "") or "")
    probability_yes = signal.fair_yes_price
    suggested_price = recommendation.suggested_price
    if probability_yes is None or suggested_price is None:
        return
    # ``suggested_price`` is side-relative: for a NO pick it is the NO entry
    # (``no_ask``), not the YES price. ``compute_kelly_sizing_diagnostics``
    # expects a YES-denominated ``price_yes`` and inverts it internally for the
    # NO side. Passing the NO entry straight through double-inverted every NO
    # recommendation — favorites pinned to the 2% max stake off a phantom edge,
    # positive-edge underdogs suppressed to zero. Map back to the YES axis so
    # the consumer's single inversion lands on the real NO price.
    if side.lower() == "no":
        price_yes = 1.0 - float(suggested_price)
    else:
        price_yes = float(suggested_price)
    try:
        from app.services.kelly_sizing_consumer import (  # noqa: PLC0415
            compute_kelly_sizing_diagnostics,
        )
        sizing = compute_kelly_sizing_diagnostics(
            db,
            probability_yes=float(probability_yes),
            price_yes=price_yes,
            side=side,
        )
    except Exception:  # noqa: BLE001
        sizing = None
    if sizing is None:
        return
    # Merge into BOTH the signal and recommendation diagnostics so
    # downstream consumers (operator UI reading either Prediction or
    # Recommendation) see the same persisted sizing decision.
    signal_diag = dict(signal.scoring_diagnostics or {})
    signal_diag["kelly_sizing"] = sizing
    signal.scoring_diagnostics = signal_diag
    rec_diag = dict(recommendation.scoring_diagnostics or {})
    rec_diag["kelly_sizing"] = sizing
    recommendation.scoring_diagnostics = rec_diag


def _persist_scored_watchlist_captures(
    db: Session,
    *,
    run_id: int,
    captures: list[ScoredWatchlistCapture],
) -> None:
    """Persist the side-effect tail of ``_score_watchlist_markets_batch``.

    Slice 6: split out so the scoring kernel above is unit-testable as a
    pure function. Iterates the captures, stages each ``SignalSnapshot``
    via ``db.add``, and routes ``capture_prediction`` calls to either the
    ``"recommendation"`` or ``"coverage"`` scope (or skips it for captures
    that were emitted purely for signal persistence).

    Smarter #9 phase 3: before persisting, enrich each capture's
    diagnostics with the suggested Kelly position size when
    applicable. The enrichment is a no-op when no bankroll is
    configured or when the recommendation was suppressed.
    """
    if not captures:
        return
    for capture in captures:
        _enrich_with_kelly_sizing(db, capture)
        db.add(capture.scored.signal)
        if capture.capture_scope is None:
            continue
        capture_prediction(
            db,
            run_id=run_id,
            event=capture.market.event,
            market=capture.market,
            recommendation=capture.scored.recommendation,
            signal=capture.scored.signal,
            metadata=capture.scored.metadata,
            capture_scope=capture.capture_scope,
        )
    db.flush()
