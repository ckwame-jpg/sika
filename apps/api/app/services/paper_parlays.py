"""Operator-built paper parlay service.

PAPER_PARLAY_SCOPE.md step 2 — ``create_paper_parlay``: resolves each
operator-supplied leg against the live market + latest prediction
data, computes the combined market price / joint probability / edge /
American odds, and persists a ``PaperParlay`` + ``PaperParlayLeg``
set atomically.

PAPER_PARLAY_SCOPE.md step 4 — ``settle_paper_parlays``: rolls up the
outcomes of each leg's source ``Prediction`` once they've all
settled. Mirrors the ``settle_parlay_predictions`` semantics from the
auto-generator (parlays.py) so paper parlays and prediction parlays
settle by the same rules.

Locked decisions from PAPER_PARLAY_SCOPE.md (see "Operator decisions"):

- **Stake is a dollar amount** (decision #1). Payout on win =
  ``stake * (1 / combined_market_price - 1)``; on loss = ``-stake``.
  Stake validation lives at the schema layer (``PaperParlayCreate``).
- **Original entry-price snapshot** (decision #3). The
  ``suggested_price`` the operator supplies per leg is what gets saved
  — even if the live market has repriced between the tray and the
  save click. Model probabilities are re-resolved from the latest
  ``Prediction`` row at save time (the operator's tray probability is
  not trusted, but the entry price they CHOSE is honored).

Settlement runs separately in step 4 (``settle_paper_parlays``); this
service only handles creation.

Correlation-adjusted quotes are computed by the same canonical engine used by
the auto generator.  Same-event relationships are intentionally reachable
here even though the auto generator's combination validator remains
cross-event-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Market, PaperParlay, PaperParlayLeg, Prediction
from app.schemas import (
    PaperParlayCreate,
    PaperParlayLegCreate,
    PaperParlayQuoteExpectation,
)
from app.services.parlay_quotes import (
    ParlayQuote,
    QuoteLeg,
    load_quote_empirical_correlations,
    quote_parlay,
)
from app.services.parlays import american_odds_from_probability
from app.services.predictions import OPEN_MARKET_STATUSES


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


MIN_LEG_COUNT = 2
MAX_LEG_COUNT = 6  # Hard cap mirroring the auto-generator's parlay_max_output.


@dataclass(slots=True)
class _ResolvedLeg:
    """A leg input enriched with the live market + prediction lookups
    needed to compute joint prob and persist the row."""

    request: PaperParlayLegCreate
    market: Market
    source_prediction: Prediction | None
    model_probability: float
    subject_name: str | None
    subject_team: str | None
    opponent: str | None
    sport_key: str | None
    event_id: int | None
    event_name: str | None
    market_kind: str | None
    market_family: str | None
    stat_key: str | None
    threshold: float | None
    fair_yes_price: float | None
    fair_no_price: float | None


def create_paper_parlay(
    db: Session,
    payload: PaperParlayCreate,
    *,
    user_id: int | None = None,
) -> PaperParlay:
    """Persist an operator-built paper parlay.

    Validation order is intentional: market existence and status are
    cheaper than prediction resolution and joint-probability math, so
    bail early to give the operator a fast 4xx if the parlay can't be
    placed at all.
    """
    resolved_legs, quote = _resolve_and_quote_legs(db, payload.legs)
    if payload.expected_quote is not None and not _quote_matches_expectation(
        quote, payload.expected_quote
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Paper parlay quote changed before save; refresh and confirm "
                "the updated quote."
            ),
        )
    american_odds = american_odds_from_probability(quote.combined_market_price)

    participating_sports = sorted(
        {leg.sport_key.upper() for leg in resolved_legs if leg.sport_key}
    )
    sport_scope = (
        participating_sports[0] if len(participating_sports) == 1 else "MIXED"
    )

    parlay = PaperParlay(
        user_id=user_id,
        stake=payload.stake,
        leg_count=len(resolved_legs),
        sport_scope=sport_scope,
        participating_sports=participating_sports,
        combined_market_price=quote.combined_market_price,
        combined_model_probability=quote.joint_probability,
        american_odds=american_odds,
        edge=quote.edge,
        notes=payload.notes,
    )
    parlay.legs = [
        PaperParlayLeg(
            leg_index=index,
            source_prediction_id=(
                leg.source_prediction.id if leg.source_prediction else None
            ),
            market_id=leg.market.id,
            ticker=leg.market.ticker,
            sport_key=leg.sport_key,
            event_name=leg.event_name,
            market_title=leg.market.title or leg.market.ticker,
            market_kind=leg.market_kind,
            stat_key=leg.stat_key,
            threshold=leg.threshold,
            subject_name=leg.subject_name,
            subject_team=leg.subject_team,
            side=leg.request.side,
            suggested_price=leg.request.suggested_price,
            fair_yes_price=leg.fair_yes_price,
            fair_no_price=leg.fair_no_price,
        )
        for index, leg in enumerate(resolved_legs)
    ]
    db.add(parlay)
    db.flush()
    return parlay


def quote_paper_parlay(
    db: Session,
    legs: list[PaperParlayLegCreate],
) -> ParlayQuote:
    """Resolve and quote legs without persisting any parlay rows."""

    _resolved_legs, quote = _resolve_and_quote_legs(db, legs)
    return quote


def _resolve_and_quote_legs(
    db: Session,
    legs: list[PaperParlayLegCreate],
) -> tuple[list[_ResolvedLeg], ParlayQuote]:
    _validate_leg_requests(legs)
    resolved_legs = [_resolve_leg(db, leg) for leg in legs]
    try:
        quote = quote_parlay(
            tuple(_quote_leg_from_resolved(leg) for leg in resolved_legs),
            tuple(leg.request.suggested_price for leg in resolved_legs),
            empirical_correlations=load_quote_empirical_correlations(db),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return resolved_legs, quote


def _validate_leg_requests(legs: list[PaperParlayLegCreate]) -> None:
    if len(legs) < MIN_LEG_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Paper parlay requires at least {MIN_LEG_COUNT} legs.",
        )
    if len(legs) > MAX_LEG_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Paper parlay accepts at most {MAX_LEG_COUNT} legs.",
        )
    tickers = [leg.ticker for leg in legs]
    if len(set(tickers)) != len(tickers):
        raise HTTPException(
            status_code=400,
            detail="Paper parlay legs must reference distinct tickers.",
        )


def _quote_leg_from_resolved(leg: _ResolvedLeg) -> QuoteLeg:
    return QuoteLeg(
        model_probability=leg.model_probability,
        sport_key=leg.sport_key,
        event_id=leg.event_id,
        subject_name=leg.subject_name,
        subject_team=leg.subject_team,
        opponent=leg.opponent,
        stat_key=leg.stat_key,
        market_family=leg.market_family,
    )


def _quote_matches_expectation(
    quote: ParlayQuote, expectation: PaperParlayQuoteExpectation
) -> bool:
    """Compare canonical six-decimal values sent back by the browser."""

    return (
        quote.combined_market_price == expectation.combined_market_price
        and quote.joint_probability == expectation.joint_probability
        and quote.edge == expectation.edge
    )


def delete_paper_parlay(
    db: Session, parlay_id: int, *, user_id: int | None = None
) -> None:
    """Permanently remove a paper parlay + its legs (cascade). Owner-only.

    Multi-user follow-up: same ownership check as the close /
    cancel flows. ``user_id=None`` skips the check for single-
    tenant deployments. Both pending and settled rows qualify.
    """
    parlay = db.get(PaperParlay, parlay_id)
    if parlay is None:
        raise HTTPException(status_code=404, detail="Paper parlay not found")
    if user_id is not None:
        if parlay.user_id is None or (parlay.user and parlay.user.is_legacy_bucket):
            raise HTTPException(
                status_code=403, detail="Legacy paper parlays are read-only.",
            )
        if parlay.user_id != user_id:
            raise HTTPException(
                status_code=403,
                detail="You can only delete parlays you saved.",
            )
    db.delete(parlay)
    db.flush()


def _resolve_leg(db: Session, leg: PaperParlayLegCreate) -> _ResolvedLeg:
    market = db.scalar(select(Market).where(Market.ticker == leg.ticker))
    if market is None:
        raise HTTPException(
            status_code=404,
            detail=f"Market not found for ticker '{leg.ticker}'.",
        )
    if market.status not in OPEN_MARKET_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Market '{leg.ticker}' is not open for trading "
                f"(status={market.status})."
            ),
        )

    # Latest unsettled Prediction for this (market, side) gives us the
    # model's current view — even though the operator's tray ENTRY
    # PRICE is locked, the joint-probability snapshot uses the live
    # model probability at save time. If no prediction row exists for
    # this leg's side (e.g. the model only generated a YES rec and the
    # operator picked NO), we treat fair_{side}_price as 1 - other_side
    # — same fallback the persistence layer uses elsewhere.
    source_prediction = db.scalar(
        select(Prediction)
        .where(
            Prediction.market_id == market.id,
            Prediction.side == leg.side,
            Prediction.settlement_status == "pending",
        )
        .order_by(Prediction.captured_at.desc())
        .limit(1)
    )
    fair_yes_price: float | None = None
    fair_no_price: float | None = None
    if source_prediction is not None:
        fair_yes_price = (
            float(source_prediction.fair_yes_price)
            if source_prediction.fair_yes_price is not None
            else None
        )
        fair_no_price = (
            float(source_prediction.fair_no_price)
            if source_prediction.fair_no_price is not None
            else None
        )
    # If the picked side has no fair price available, the joint prob
    # can't be meaningfully computed. Refuse rather than silently
    # falling back to 0.5 (which would pollute the edge calculation).
    model_probability = _model_probability_for_side(
        side=leg.side,
        fair_yes_price=fair_yes_price,
        fair_no_price=fair_no_price,
    )
    if model_probability is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No model probability available for '{leg.ticker}' "
                f"{leg.side.upper()}. The operator's tray must have been "
                "built from a stale slate — refresh and re-pick."
            ),
        )

    metadata = dict(market.raw_data or {})
    subject_name = (
        str(metadata.get("copilot_subject_name") or "").strip() or None
    )
    subject_team = (
        str(metadata.get("copilot_subject_team") or "").strip().upper() or None
    )

    event = market.event
    sport_key = (
        (market.sport_key or (event.sport_key if event else None) or "").upper()
        or None
    )
    event_name = event.name if event else None

    opponent: str | None = None
    if event is not None and subject_team:
        for participant_link in event.participants:
            participant = participant_link.participant
            full_name = (participant.display_name or "").upper()
            short_name = (participant.short_name or "").upper()
            if subject_team in {full_name, short_name}:
                continue
            opponent = short_name or full_name or None
            if opponent:
                break

    return _ResolvedLeg(
        request=leg,
        market=market,
        source_prediction=source_prediction,
        model_probability=model_probability,
        subject_name=subject_name,
        subject_team=subject_team,
        opponent=opponent,
        sport_key=sport_key,
        event_id=event.id if event is not None else None,
        event_name=event_name,
        market_kind=(
            str(metadata.get("copilot_market_kind") or "").strip() or None
        ),
        market_family=(
            str(
                metadata.get("copilot_market_family")
                or metadata.get("copilot_market_kind")
                or ""
            ).strip()
            or None
        ),
        stat_key=str(metadata.get("copilot_stat_key") or "").strip() or None,
        threshold=_safe_float(metadata.get("copilot_threshold")),
        fair_yes_price=fair_yes_price,
        fair_no_price=fair_no_price,
    )


def _model_probability_for_side(
    *,
    side: str,
    fair_yes_price: float | None,
    fair_no_price: float | None,
) -> float | None:
    """Return P(picked side) using the side-aware fair price.

    Mirrors ``parlays._selected_model_probability``. The fallback when
    one side is missing reads from the complement of the other — same
    pattern the scoring layer's ``_signal_snapshot_from_prediction``
    uses for predictions that only store one side's fair price.
    """
    if side == "yes":
        if fair_yes_price is not None:
            return fair_yes_price
        if fair_no_price is not None:
            return 1.0 - fair_no_price
        return None
    if side == "no":
        if fair_no_price is not None:
            return fair_no_price
        if fair_yes_price is not None:
            return 1.0 - fair_yes_price
        return None
    return None


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# -----------------------------------------------------------------------------
# Settlement (PAPER_PARLAY_SCOPE.md step 4)
# -----------------------------------------------------------------------------

# Terminal outcome values written to ``PaperParlay.outcome``. Mirrors
# the auto-generator's parlay-prediction vocabulary (parlays.py) so a
# single set of UI display logic can render both surfaces.
OUTCOME_PENDING = "pending"
OUTCOME_WON = "won"
OUTCOME_LOST = "lost"
OUTCOME_PUSH = "push"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_UNRESOLVED = "unresolved"


def _empty_settlement_summary() -> dict[str, int]:
    return {
        "processed": 0,
        "updated": 0,
        "pending": 0,
        "won": 0,
        "lost": 0,
        "cancelled": 0,
        "unresolved": 0,
    }


def settle_paper_parlays(db: Session) -> dict[str, int]:
    """Roll up settlement on every pending paper parlay.

    For each parlay still in ``settlement_status == "pending"``:

    - If any leg's ``source_prediction`` is missing (e.g. the prediction
      row was pruned), mark the parlay ``unresolved`` with a note.
    - If every leg's source prediction has a ``won`` outcome →
      ``outcome=won``, ``realized_pnl = stake * (1/combined_market_price - 1)``.
    - If ANY leg lost → ``outcome=lost``, ``realized_pnl = -stake``.
    - If any leg pushed/cancelled (but none lost) → ``outcome=cancelled``,
      ``realized_pnl=0`` (sportsbook convention varies; treating the
      whole parlay as cancelled is the conservative paper choice).
    - If any source prediction is itself ``unresolved`` → mirror that.
    - Otherwise (at least one leg still pending) → leave pending.

    Codex pattern 4 (reduction reuse): the same ``any() / all()``
    aggregation the auto-generator uses, applied here so paper +
    prediction parlays settle by identical rules. If the auto-
    generator's rollup changes (e.g. adds a "half-won" outcome), the
    regression tests below will surface the divergence loudly.
    """
    summary = _empty_settlement_summary()
    pending_parlays = db.scalars(
        select(PaperParlay)
        .where(PaperParlay.settlement_status == "pending")
        .order_by(PaperParlay.created_at.asc(), PaperParlay.id.asc())
    ).all()
    summary["processed"] = len(pending_parlays)
    if not pending_parlays:
        return summary

    now = _now_utc()
    for parlay in pending_parlays:
        source_predictions = [
            leg.source_prediction for leg in parlay.legs if leg.source_prediction is not None
        ]
        # Codex pattern 5 (reset edge cases): if any leg lost its
        # source prediction (the row was pruned, the FK was nulled),
        # we can't settle. Mark unresolved with a note. Same pattern
        # as parlays._settle_parlay_rows.
        if len(source_predictions) != len(parlay.legs):
            _apply_unresolved(
                parlay,
                summary,
                notes="One or more source leg predictions are missing.",
            )
            continue

        outcomes = [pred.prediction_outcome for pred in source_predictions]
        statuses = [pred.settlement_status for pred in source_predictions]

        if any(outcome == "lost" for outcome in outcomes):
            parlay.settlement_status = "settled"
            parlay.outcome = OUTCOME_LOST
            parlay.realized_pnl = round(-parlay.stake, 4)
            parlay.settled_at = now
            parlay.settlement_notes = "At least one leg settled as a loss."
            summary["updated"] += 1
            summary["lost"] += 1
            continue

        if outcomes and all(outcome == "won" for outcome in outcomes):
            parlay.settlement_status = "settled"
            parlay.outcome = OUTCOME_WON
            # Decision #1 (dollar stake): payout on win =
            # ``stake * (1 / combined_market_price - 1)``. The "1/p - 1"
            # factor is the parlay's decimal-odds profit per unit
            # wagered (decimal odds = 1/p; profit = odds - 1).
            #
            # Codex pattern 6 (implicit data shape): combined_market_price
            # comes from the saved snapshot which is bounded (0, 1)
            # by the schema; no division-by-zero guard needed at
            # runtime as long as that invariant holds. If a future
            # caller bypasses the schema and writes 0, this will
            # raise ZeroDivisionError and surface in the cron logs
            # rather than silently producing infinity.
            # Defensive: a legacy row created before the creation-time guard
            # could carry combined_market_price == 0. Book zero rather than
            # raise ZeroDivisionError and abort the whole settlement pass.
            if parlay.combined_market_price and parlay.combined_market_price > 0.0:
                profit = parlay.stake * (1.0 / parlay.combined_market_price - 1.0)
            else:
                profit = 0.0
            parlay.realized_pnl = round(profit, 4)
            parlay.settled_at = now
            parlay.settlement_notes = "Every leg settled as a win."
            summary["updated"] += 1
            summary["won"] += 1
            continue

        # Only void the whole parlay on a cancel/push once no leg is still
        # pending or unresolved. Otherwise the parlay finalizes as cancelled
        # mid-slate and a leg that later loses (checked first, above) can never
        # flip it to lost — silently inflating P&L.
        leg_still_open = any(
            s == "pending" or o == "pending" or s == "unresolved" or o == "unresolved"
            for s, o in zip(statuses, outcomes, strict=False)
        )
        if not leg_still_open and any(outcome in {"cancelled", "push"} for outcome in outcomes):
            parlay.settlement_status = "settled"
            parlay.outcome = OUTCOME_CANCELLED
            parlay.realized_pnl = 0.0
            parlay.settled_at = now
            parlay.settlement_notes = (
                "At least one leg cancelled or pushed, so the parlay was cancelled."
            )
            summary["updated"] += 1
            summary["cancelled"] += 1
            continue

        if any(
            status == "unresolved" or outcome == "unresolved"
            for status, outcome in zip(statuses, outcomes, strict=False)
        ):
            _apply_unresolved(
                parlay,
                summary,
                notes="One or more legs left the open state without a final settlement result.",
            )
            continue

        # At least one leg still pending → don't touch the parlay.
        summary["pending"] += 1

    db.flush()
    return summary


def _apply_unresolved(
    parlay: PaperParlay, summary: dict[str, int], *, notes: str
) -> None:
    """Apply the ``unresolved`` outcome, but only count the row as
    updated when its state actually changed. Codex pattern 5 / bug
    #27 framing: a parlay already in ``unresolved`` from a prior
    settlement pass shouldn't re-bump the operator-facing
    ``updated`` counter on every cron tick."""
    needs_update = (
        parlay.settlement_status != "pending"  # already settled means no-op
        or parlay.outcome != OUTCOME_UNRESOLVED
        or parlay.settlement_notes != notes
    )
    # NOTE: pending parlays move TO unresolved here; we keep
    # ``settlement_status = "pending"`` so the next cron tick re-evaluates
    # if the upstream prediction rows reappear / re-settle. That mirrors
    # the auto-generator's behavior (parlays.py:719-728): unresolved is
    # a soft state, not a terminal one.
    if needs_update:
        parlay.outcome = OUTCOME_UNRESOLVED
        parlay.settlement_notes = notes
        summary["updated"] += 1
    summary["unresolved"] += 1
