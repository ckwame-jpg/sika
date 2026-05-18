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

The correlation-adjusted joint probability re-uses the same math the
auto-parlay generator uses (``_correlation_adjusted_joint_probability``
in ``parlays.py``). To avoid a fragile private-import coupling, the
formula is reproduced locally with a comment pointing at the
authoritative source. If the auto-generator updates its formula and
this copy drifts, the regression tests below (which pin both the
independent product and the correlation lift) will catch it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import prod

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Market, PaperParlay, PaperParlayLeg, Prediction
from app.schemas import PaperParlayCreate, PaperParlayLegCreate
from app.services.parlays import american_odds_from_probability
from app.services.predictions import OPEN_MARKET_STATUSES


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


MIN_LEG_COUNT = 2
MAX_LEG_COUNT = 6  # Hard cap mirroring the auto-generator's parlay_max_output.

# Same per-pair weights the auto-generator uses (parlays.py
# ``_pair_weight`` theoretical priors). Duplicated here to avoid
# importing the underscore-prefixed helper. If you change one, change
# both — the regression test pins these by value.
_PAIR_WEIGHT_SHARED_SUBJECT = 0.7
_PAIR_WEIGHT_SAME_TEAM = 0.3
_PAIR_WEIGHT_SHARED_OPPONENT = 0.2
_CORRELATION_CAP = 0.85


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
    event_name: str | None
    market_kind: str | None
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
    if len(payload.legs) < MIN_LEG_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Paper parlay requires at least {MIN_LEG_COUNT} legs.",
        )
    if len(payload.legs) > MAX_LEG_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"Paper parlay accepts at most {MAX_LEG_COUNT} legs.",
        )

    # Duplicate-ticker check happens here, before any DB lookups, so a
    # bad payload is rejected before we spend round trips on it.
    tickers = [leg.ticker for leg in payload.legs]
    if len(set(tickers)) != len(tickers):
        raise HTTPException(
            status_code=400,
            detail="Paper parlay legs must reference distinct tickers.",
        )

    resolved_legs = [_resolve_leg(db, leg) for leg in payload.legs]

    combined_market_price = round(
        prod(leg.request.suggested_price for leg in resolved_legs),
        6,
    )
    combined_model_probability = round(
        _correlation_adjusted_joint(resolved_legs),
        6,
    )
    edge = round(combined_model_probability - combined_market_price, 6)
    american_odds = american_odds_from_probability(combined_market_price)

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
        combined_market_price=combined_market_price,
        combined_model_probability=combined_model_probability,
        american_odds=american_odds,
        edge=edge,
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


def delete_paper_parlay(
    db: Session, parlay_id: int, *, user_id: int | None = None
) -> None:
    """Permanently remove a paper parlay + its legs (cascade). Owner-only.

    Multi-user follow-up: same ownership check as the close /
    cancel flows. ``user_id=None`` skips the check for single-
    tenant deployments. Deletes regardless of settlement status —
    pending parlays, settled wins/losses, all qualify.
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
        event_name=event_name,
        market_kind=metadata.get("copilot_market_kind"),
        stat_key=metadata.get("copilot_stat_key"),
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


def _correlation_adjusted_joint(legs: list[_ResolvedLeg]) -> float:
    """Joint probability with positive-correlation lift.

    Mirrors ``parlays._correlation_adjusted_joint_probability``:
    independent product as the lower bound, ``min(leg_probs)`` as the
    upper bound, lifted by a per-pair-weighted blend capped at
    ``_CORRELATION_CAP``. The formula's rationale is in the
    auto-generator docstring; the regression tests here pin the
    expected lift behavior so a drift between the two implementations
    is caught at test time.
    """
    leg_probs = [leg.model_probability for leg in legs]
    independent = prod(leg_probs)
    if len(leg_probs) <= 1:
        return float(independent)
    min_leg = min(leg_probs)

    pairs = _count_correlation_pairs(legs)
    total_pairs = len(leg_probs) * (len(leg_probs) - 1) // 2
    weighted = (
        _PAIR_WEIGHT_SHARED_SUBJECT * pairs["shared_subject"]
        + _PAIR_WEIGHT_SAME_TEAM * pairs["same_team"]
        + _PAIR_WEIGHT_SHARED_OPPONENT * pairs["shared_opponent"]
    ) / max(total_pairs, 1)
    correlation_factor = min(weighted, _CORRELATION_CAP)
    return float(independent + correlation_factor * (min_leg - independent))


def _count_correlation_pairs(legs: list[_ResolvedLeg]) -> dict[str, int]:
    """Count pairwise correlation overlaps across legs.

    A leg pair counts toward at most ONE correlation category — same
    subject (strongest) > same team > shared opponent — so we don't
    double-count a pair where multiple keys overlap. Mirrors the
    pairwise reduction in ``parlays._count_correlation_pairs``.
    """
    counts = {"shared_subject": 0, "same_team": 0, "shared_opponent": 0}
    n = len(legs)
    for i in range(n):
        for j in range(i + 1, n):
            left = legs[i]
            right = legs[j]
            if (
                left.subject_name
                and right.subject_name
                and left.subject_name.lower() == right.subject_name.lower()
            ):
                counts["shared_subject"] += 1
            elif (
                left.subject_team
                and right.subject_team
                and left.subject_team == right.subject_team
            ):
                counts["same_team"] += 1
            elif (
                left.opponent
                and right.opponent
                and left.opponent == right.opponent
            ):
                counts["shared_opponent"] += 1
    return counts


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
            profit = parlay.stake * (1.0 / parlay.combined_market_price - 1.0)
            parlay.realized_pnl = round(profit, 4)
            parlay.settled_at = now
            parlay.settlement_notes = "Every leg settled as a win."
            summary["updated"] += 1
            summary["won"] += 1
            continue

        if any(outcome in {"cancelled", "push"} for outcome in outcomes):
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
