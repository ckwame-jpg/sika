"""Watchlist generation orchestration — the pipeline that wraps
the per-market kernel into a full slate-scoring run.

Extracted from ``scoring/__init__.py`` as part of R1 phase 4. The
orchestration entry points compose the rest of the scoring package:

- ``_score_watchlist_markets_batch`` — pure batch scorer that turns
  ``list[Market]`` into ``list[ScoredWatchlistCapture]`` without
  side effects.
- ``stage_*`` — batch-then-persist wrappers used by the staged
  refresh pipeline.
- ``finalize_staged_watchlist`` / ``finalize_current_slate_watchlist``
  — apply monotonicity + dedupe to captured predictions, promote
  winners into ``Recommendation`` rows, snapshot parlays.
- ``regenerate_watchlist`` — single-shot variant used by the
  test-mode ``/ops/jobs/refresh-watchlist`` path; same logic as
  the staged pipeline but in-process rather than batched.

Also moved here for cohesion:

- ``_record_scorer_outcome`` / ``_record_candidate_filter`` /
  ``_merge_count_maps`` — counter helpers for
  ``WatchlistGenerationSummary``.
- ``_scoring_none_reason`` / ``_suppression_outcome_reason`` —
  classify why a market didn't produce a recommendation (used by
  the scorer-outcome bookkeeping).
- ``_maintenance_watchlist_market_batch`` /
  ``_explicit_watchlist_market_batch`` —
  DB batch loaders that feed ``stage_*``.
- ``_annotate_current_watchlist_flag`` — mutator that stamps the
  current-slate flag onto signal + recommendation diagnostics.

The orchestrators reach into ``_build_scored_recommendation`` (the
kernel entry point in ``__init__.py``) via a lazy import: deferring
that import until call time breaks the otherwise-circular
``__init__.py`` → ``orchestration.py`` → ``__init__.py`` graph.
A future R1 phase that pulls ``_build_scored_recommendation`` into
its own kernel module can replace the lazy import with a normal
one.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import (
    Event,
    EventParticipant,
    Market,
    Prediction,
    Recommendation,
)
from app.services.parlays import (
    ParlayCandidateInput,
    capture_parlay_artifacts,
    clear_active_parlay_watchlist,
)
from app.services.predictions import OPEN_MARKET_STATUSES, capture_prediction
from app.services.scoring.heuristics import _market_metadata
from app.services.scoring.monotonicity import (
    _apply_prediction_monotonicity,
    _dedupe_prediction_recommendations,
    _dedupe_winner_recommendations,
    _enforce_prop_monotonicity,
)
from app.services.scoring.persistence import (
    _build_recommendation_from_prediction,
    _parlay_candidate_from_prediction,
    _persist_scored_watchlist_captures,
)
from app.services.scoring.resolver import PropStatsResolver
from app.services.scoring.types import (
    ScoredRecommendation,
    ScoredWatchlistCapture,
    WatchlistGenerationSummary,
)
from app.services.watchlist_coverage import (
    CURRENT_WATCHLIST_SPORTS,
    current_watchlist_event_ids,
    is_current_watchlist_market,
    latest_snapshot_by_market_id,
)

__all__ = [
    "_record_scorer_outcome",
    "_record_candidate_filter",
    "_merge_count_maps",
    "_scoring_none_reason",
    "_suppression_outcome_reason",
    "_maintenance_watchlist_market_batch",
    "_explicit_watchlist_market_batch",
    "_annotate_current_watchlist_flag",
    "_score_watchlist_markets_batch",
    "stage_maintenance_watchlist_batch",
    "stage_current_slate_watchlist_batch",
    "finalize_staged_watchlist",
    "finalize_current_slate_watchlist",
    "regenerate_watchlist",
]


# -- Summary counter helpers -------------------------------------------


def _record_scorer_outcome(summary: WatchlistGenerationSummary, reason: str) -> None:
    summary.outcome_reason_counts[reason] = summary.outcome_reason_counts.get(reason, 0) + 1


def _record_candidate_filter(summary: WatchlistGenerationSummary, reason: str) -> None:
    summary.filtered_candidate_market_count += 1
    summary.candidate_filter_reason_counts[reason] = summary.candidate_filter_reason_counts.get(reason, 0) + 1


def _merge_count_maps(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    merged = dict(left)
    for key, value in right.items():
        merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


# -- Scorer-outcome reason classifiers ---------------------------------


def _scoring_none_reason(market: Market) -> str:
    if market.event is None:
        return "mapping_failed"
    metadata = _market_metadata(market)
    family = str(metadata.get("copilot_market_family") or "")
    if family not in {"winner", "game_line", "player_prop"}:
        return "unsupported_shape"
    if family == "player_prop":
        subject_name = str(metadata.get("copilot_subject_name") or "").strip()
        stat_key = str(metadata.get("copilot_stat_key") or "").strip()
        threshold = float(metadata.get("copilot_threshold") or 0.0)
        if not subject_name or not stat_key or threshold <= 0:
            return "unsupported_shape"
        return "prop_context_missing"
    return "scoring_returned_none"


def _suppression_outcome_reason(scored: ScoredRecommendation, *, current_watchlist_market: bool) -> str:
    diagnostics = dict(scored.signal.scoring_diagnostics or {})
    suppression_reasons = {str(value) for value in list(diagnostics.get("suppression_reasons") or [])}
    if "critical_market_snapshot_missing" in suppression_reasons:
        return "missing_snapshot"
    if "player_not_in_starting_lineup" in suppression_reasons:
        return "suppressed_player_not_in_starting_lineup"
    if "player_injury_out" in suppression_reasons:
        return "suppressed_player_injury_out"
    if "player_injury_doubtful" in suppression_reasons:
        return "suppressed_player_injury_doubtful"
    if "model_book_disagreement" in suppression_reasons:
        return "suppressed_model_book_disagreement"
    if "no_side_not_actionable_on_kalshi" in suppression_reasons:
        return "suppressed_no_side_not_actionable"
    if "min_edge" in suppression_reasons or "yes_side_negative_edge" in suppression_reasons:
        return "suppressed_min_edge"
    if "min_confidence" in suppression_reasons:
        return "suppressed_min_confidence"
    if not current_watchlist_market:
        return "not_current_slate"
    return "coverage"


# -- Batch loaders -----------------------------------------------------


def _maintenance_watchlist_market_batch(
    db: Session,
    *,
    cursor_market_id: int | None = None,
    batch_size: int = 100,
) -> list[Market]:
    stmt = (
        select(Market)
        .options(joinedload(Market.event).selectinload(Event.participants).joinedload(EventParticipant.participant))
        .where(Market.event_id.is_not(None), Market.status.in_(tuple(OPEN_MARKET_STATUSES)))
        .order_by(Market.id.asc())
    )
    if cursor_market_id is not None:
        stmt = stmt.where(Market.id > cursor_market_id)
    return db.scalars(stmt.limit(batch_size)).all()


def _explicit_watchlist_market_batch(
    db: Session,
    *,
    market_ids: list[int],
    cursor_index: int = 0,
    batch_size: int = 100,
) -> tuple[list[Market], WatchlistGenerationSummary, int | None, bool]:
    summary = WatchlistGenerationSummary()
    batch_ids = market_ids[cursor_index : cursor_index + batch_size]
    next_index = cursor_index + len(batch_ids)
    complete = next_index >= len(market_ids)
    if not batch_ids:
        return [], summary, None, True

    current_event_ids = set(current_watchlist_event_ids(db))
    rows = db.scalars(
        select(Market)
        .options(joinedload(Market.event).selectinload(Event.participants).joinedload(EventParticipant.participant))
        .where(Market.id.in_(tuple(sorted(batch_ids))))
    ).all()
    by_id = {market.id: market for market in rows}
    loaded_by_id: dict[int, Market] = {}
    for market_id in batch_ids:
        market = by_id.get(market_id)
        if market is None:
            _record_candidate_filter(summary, "not_found")
            continue
        if market.event_id is None or market.event is None:
            _record_candidate_filter(summary, "event_missing")
            continue
        if (market.sport_key or "").upper() not in CURRENT_WATCHLIST_SPORTS:
            _record_candidate_filter(summary, "sport_not_supported")
            continue
        if (market.status or "").lower() not in OPEN_MARKET_STATUSES:
            _record_candidate_filter(summary, "status_not_open")
            continue
        if market.event_id not in current_event_ids:
            _record_candidate_filter(summary, "not_current_event")
            continue
        loaded_by_id[market_id] = market

    ordered = [loaded_by_id[market_id] for market_id in batch_ids if market_id in loaded_by_id]
    summary.loaded_candidate_market_count = len(ordered)
    return ordered, summary, (None if complete else next_index), complete


def _annotate_current_watchlist_flag(scored: ScoredRecommendation, *, current_watchlist_market: bool) -> None:
    signal_diagnostics = dict(scored.signal.scoring_diagnostics or {})
    signal_diagnostics["current_watchlist_market"] = current_watchlist_market
    scored.signal.scoring_diagnostics = signal_diagnostics
    if scored.recommendation is not None:
        recommendation_diagnostics = dict(scored.recommendation.scoring_diagnostics or {})
        recommendation_diagnostics["current_watchlist_market"] = current_watchlist_market
        scored.recommendation.scoring_diagnostics = recommendation_diagnostics


# -- Batch scorer + stage / finalize / regenerate -----------------------


def _score_watchlist_markets_batch(
    db: Session,
    *,
    markets: list[Market],
    resolver: PropStatsResolver | None = None,
) -> tuple[WatchlistGenerationSummary, list[ScoredWatchlistCapture]]:
    """Score a batch of markets without persisting anything.

    Slice 6: this is the pure half of the watchlist batch pipeline. It
    consults the latest market snapshots, runs ``_build_scored_recommendation``
    over each market, and updates the summary's diagnostic counters based
    on the scored output. It does **not** call ``db.add`` or
    ``capture_prediction``; the caller is responsible for handing the
    returned captures to ``_persist_scored_watchlist_captures``.
    """
    # R1 phase 4: ``_build_scored_recommendation`` still lives in
    # ``scoring/__init__.py``. Lazy-import here breaks the otherwise-
    # circular package init graph. A future kernel-extraction phase
    # can move it into ``scoring/kernel.py`` and switch to a normal
    # top-level import.
    from app.services.scoring import _build_scored_recommendation

    summary = WatchlistGenerationSummary()
    captures: list[ScoredWatchlistCapture] = []
    if not markets:
        return summary, captures
    active_resolver = resolver or PropStatsResolver(db, allow_network=False)

    latest_snapshots = latest_snapshot_by_market_id(db, [market.id for market in markets])
    for market in markets:
        if not market.event:
            _record_scorer_outcome(summary, "mapping_failed")
            continue
        latest_snapshot = latest_snapshots.get(market.id)
        scored = _build_scored_recommendation(db, market.event, market, latest_snapshot, resolver=active_resolver)
        if scored is None:
            _record_scorer_outcome(summary, _scoring_none_reason(market))
            continue
        summary.scored_market_count += 1
        current_watchlist_market = is_current_watchlist_market(market)
        _annotate_current_watchlist_flag(scored, current_watchlist_market=current_watchlist_market)
        if scored.recommendation is not None:
            _record_scorer_outcome(summary, "recommended")
            captures.append(
                ScoredWatchlistCapture(
                    market=market,
                    scored=scored,
                    capture_scope="recommendation",
                )
            )
            summary.prediction_count += 1
            continue
        _record_scorer_outcome(
            summary,
            _suppression_outcome_reason(scored, current_watchlist_market=current_watchlist_market),
        )
        diagnostics = dict(scored.signal.scoring_diagnostics or {})
        suppression_reasons = {str(value) for value in list(diagnostics.get("suppression_reasons") or [])}
        if "winner_selected_probability_floor" in suppression_reasons:
            summary.heuristic_longshots_suppressed += 1
        if "critical_market_snapshot_missing" in suppression_reasons:
            summary.critical_context_suppressed += 1
        if current_watchlist_market:
            captures.append(
                ScoredWatchlistCapture(
                    market=market,
                    scored=scored,
                    capture_scope="coverage",
                )
            )
            summary.prediction_count += 1
            summary.coverage_prediction_count += 1
        else:
            # The scored signal still needs to be persisted even when no
            # prediction is captured — production behavior pre-Slice-6 always
            # called ``db.add(scored.signal)`` for every successfully-scored
            # market regardless of whether ``capture_prediction`` ran.
            captures.append(
                ScoredWatchlistCapture(
                    market=market,
                    scored=scored,
                    capture_scope=None,
                )
            )
    return summary, captures


def stage_maintenance_watchlist_batch(
    db: Session,
    *,
    run_id: int,
    resolver: PropStatsResolver | None = None,
    cursor_market_id: int | None = None,
    batch_size: int = 100,
) -> tuple[WatchlistGenerationSummary, int | None, bool]:
    markets = _maintenance_watchlist_market_batch(
        db,
        cursor_market_id=cursor_market_id,
        batch_size=batch_size,
    )
    if not markets:
        return WatchlistGenerationSummary(), None, True

    summary, captures = _score_watchlist_markets_batch(
        db,
        markets=markets,
        resolver=resolver,
    )
    _persist_scored_watchlist_captures(db, run_id=run_id, captures=captures)
    return summary, markets[-1].id if markets else cursor_market_id, len(markets) < batch_size


def stage_current_slate_watchlist_batch(
    db: Session,
    *,
    run_id: int,
    market_ids: list[int],
    resolver: PropStatsResolver | None = None,
    cursor_index: int = 0,
    batch_size: int = 100,
) -> tuple[WatchlistGenerationSummary, int | None, bool]:
    markets, batch_summary, next_index, complete = _explicit_watchlist_market_batch(
        db,
        market_ids=market_ids,
        cursor_index=cursor_index,
        batch_size=batch_size,
    )
    if not markets:
        return batch_summary, next_index, complete
    scoring_summary, captures = _score_watchlist_markets_batch(
        db,
        markets=markets,
        resolver=resolver,
    )
    scoring_summary.loaded_candidate_market_count += batch_summary.loaded_candidate_market_count
    scoring_summary.filtered_candidate_market_count += batch_summary.filtered_candidate_market_count
    scoring_summary.candidate_filter_reason_counts = _merge_count_maps(
        scoring_summary.candidate_filter_reason_counts,
        batch_summary.candidate_filter_reason_counts,
    )
    _persist_scored_watchlist_captures(db, run_id=run_id, captures=captures)
    return scoring_summary, next_index, complete


def finalize_staged_watchlist(
    db: Session,
    *,
    run_id: int,
    capture_parlays: bool = True,
) -> WatchlistGenerationSummary:
    summary = WatchlistGenerationSummary()
    predictions = db.scalars(
        select(Prediction)
        .options(
            joinedload(Prediction.market)
            .joinedload(Market.event)
            .selectinload(Event.participants)
            .joinedload(EventParticipant.participant)
        )
        .where(Prediction.run_id == run_id)
        .order_by(Prediction.id.asc())
    ).all()

    candidate_predictions = [prediction for prediction in predictions if prediction.capture_scope != "coverage"]
    _apply_prediction_monotonicity(candidate_predictions, summary=summary)
    candidate_predictions = [prediction for prediction in predictions if prediction.capture_scope not in {"coverage", "suppressed"}]
    winners, collapsed_count, combo_suppressed = _dedupe_prediction_recommendations(candidate_predictions)
    summary.inverse_winner_duplicates_collapsed = collapsed_count
    summary.combo_prop_candidates_suppressed = combo_suppressed
    winner_ids = {prediction.id for prediction in winners}

    for prediction in predictions:
        if prediction.capture_scope == "coverage":
            continue
        if prediction.id in winner_ids:
            prediction.capture_scope = "recommendation"
            continue
        current_flag = bool((prediction.scoring_diagnostics or {}).get("current_watchlist_market"))
        if current_flag:
            prediction.capture_scope = "coverage"
            continue
        db.delete(prediction)

    db.flush()
    db.query(Recommendation).delete()
    if capture_parlays:
        clear_active_parlay_watchlist(db)
    db.flush()

    parlay_candidates: list[ParlayCandidateInput] = []
    for prediction in winners:
        diagnostics = dict(prediction.scoring_diagnostics or {})
        quality_tier = str(diagnostics.get("quality_tier") or "medium")
        summary.quality_tier_counts[quality_tier] = summary.quality_tier_counts.get(quality_tier, 0) + 1
        if str(diagnostics.get("source_type") or "") == "combo_derived":
            summary.combo_prop_candidates_emitted += 1
        db.add(_build_recommendation_from_prediction(prediction))
        summary.recommendation_count += 1
        candidate = _parlay_candidate_from_prediction(prediction)
        if candidate is not None:
            parlay_candidates.append(candidate)

    db.flush()
    if capture_parlays:
        parlay_recommendation_count, parlay_prediction_count = capture_parlay_artifacts(
            db,
            run_id=run_id,
            candidates=parlay_candidates,
        )
        summary.parlay_recommendation_count = parlay_recommendation_count
        summary.parlay_prediction_count = parlay_prediction_count
    db.flush()
    summary.prediction_count = int(
        db.scalar(select(func.count()).select_from(Prediction).where(Prediction.run_id == run_id))
        or 0
    )
    summary.scored_market_count = len(predictions)
    summary.coverage_prediction_count = int(
        db.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.run_id == run_id, Prediction.capture_scope == "coverage")
        )
        or 0
    )
    return summary


def finalize_current_slate_watchlist(
    db: Session,
    *,
    run_id: int,
    candidate_market_ids: set[int],
    staged_summary: WatchlistGenerationSummary | None = None,
) -> WatchlistGenerationSummary:
    summary = WatchlistGenerationSummary()
    predictions = db.scalars(
        select(Prediction)
        .options(
            joinedload(Prediction.market)
            .joinedload(Market.event)
            .selectinload(Event.participants)
            .joinedload(EventParticipant.participant)
        )
        .where(Prediction.run_id == run_id)
        .order_by(Prediction.id.asc())
    ).all()

    candidate_predictions = [prediction for prediction in predictions if prediction.capture_scope != "coverage"]
    # Bug #9 follow-up (codex PR #33 round-5 P2): in the staged path the
    # original ``recommended`` counts live in ``staged_summary``, not the
    # fresh summary this function builds. Apply the metric adjustment to
    # ``staged_summary`` so the merge in ingestion.py doesn't double-count
    # a market as both recommended *and* suppressed.
    monotonicity_metric_target = staged_summary if staged_summary is not None else summary
    _apply_prediction_monotonicity(candidate_predictions, summary=monotonicity_metric_target)
    candidate_predictions = [prediction for prediction in predictions if prediction.capture_scope not in {"coverage", "suppressed"}]
    winners, collapsed_count, combo_suppressed = _dedupe_prediction_recommendations(candidate_predictions)
    summary.inverse_winner_duplicates_collapsed = collapsed_count
    summary.combo_prop_candidates_suppressed = combo_suppressed
    winner_ids = {prediction.id for prediction in winners}

    for prediction in predictions:
        if prediction.capture_scope == "coverage":
            continue
        if prediction.id in winner_ids:
            prediction.capture_scope = "recommendation"
            continue
        current_flag = bool((prediction.scoring_diagnostics or {}).get("current_watchlist_market"))
        if current_flag:
            prediction.capture_scope = "coverage"
            continue
        db.delete(prediction)

    db.flush()
    if candidate_market_ids:
        db.query(Recommendation).filter(
            Recommendation.market_id.in_(tuple(sorted(candidate_market_ids)))
        ).delete(synchronize_session=False)
    db.flush()

    for prediction in winners:
        diagnostics = dict(prediction.scoring_diagnostics or {})
        quality_tier = str(diagnostics.get("quality_tier") or "medium")
        summary.quality_tier_counts[quality_tier] = summary.quality_tier_counts.get(quality_tier, 0) + 1
        if str(diagnostics.get("source_type") or "") == "combo_derived":
            summary.combo_prop_candidates_emitted += 1
        db.add(_build_recommendation_from_prediction(prediction))
        summary.recommendation_count += 1

    db.flush()
    summary.prediction_count = int(
        db.scalar(select(func.count()).select_from(Prediction).where(Prediction.run_id == run_id))
        or 0
    )
    return summary


def regenerate_watchlist(
    db: Session,
    *,
    run_id: int | None = None,
    resolver: PropStatsResolver | None = None,
    allowed_market_ids: set[int] | None = None,
    replace_all: bool = True,
    capture_parlays: bool = True,
    candidate_markets: list[Market] | None = None,
) -> WatchlistGenerationSummary:
    # Same lazy-import escape as ``_score_watchlist_markets_batch``.
    from app.services.scoring import _build_scored_recommendation

    if replace_all:
        db.query(Recommendation).delete()
        if capture_parlays:
            clear_active_parlay_watchlist(db)
    elif allowed_market_ids:
        db.query(Recommendation).filter(Recommendation.market_id.in_(tuple(sorted(allowed_market_ids)))).delete(synchronize_session=False)
    summary = WatchlistGenerationSummary()
    active_resolver = resolver or PropStatsResolver(db, allow_network=False)
    parlay_candidates: list[ParlayCandidateInput] = []
    pending_recommendations: list[tuple[Market, ScoredRecommendation]] = []
    current_coverage_candidates: list[tuple[Market, ScoredRecommendation]] = []
    if candidate_markets is not None:
        markets = [
            market
            for market in candidate_markets
            if market.event_id is not None and (market.status or "").lower() in OPEN_MARKET_STATUSES
        ]
        if allowed_market_ids is not None:
            if not allowed_market_ids:
                return summary
            markets = [market for market in markets if market.id in allowed_market_ids]
    else:
        stmt = (
            select(Market)
            .options(joinedload(Market.event).selectinload(Event.participants).joinedload(EventParticipant.participant))
            .where(Market.event_id.is_not(None), Market.status.in_(tuple(OPEN_MARKET_STATUSES)))
        )
        if allowed_market_ids is not None:
            if not allowed_market_ids:
                return summary
            stmt = stmt.where(Market.id.in_(tuple(sorted(allowed_market_ids))))
        markets = db.scalars(stmt).all()
    latest_snapshots = latest_snapshot_by_market_id(db, [market.id for market in markets])
    for market in markets:
        if not market.event:
            continue
        latest_snapshot = latest_snapshots.get(market.id)
        scored = _build_scored_recommendation(db, market.event, market, latest_snapshot, resolver=active_resolver)
        if scored:
            summary.scored_market_count += 1
            db.add(scored.signal)
            current_watchlist_market = is_current_watchlist_market(market)
            if current_watchlist_market:
                current_coverage_candidates.append((market, scored))
            if scored.recommendation:
                _record_scorer_outcome(summary, "recommended")
                pending_recommendations.append((market, scored))
            else:
                _record_scorer_outcome(
                    summary,
                    _suppression_outcome_reason(scored, current_watchlist_market=current_watchlist_market),
                )
                diagnostics = dict(scored.signal.scoring_diagnostics or {})
                suppression_reasons = {str(value) for value in list(diagnostics.get("suppression_reasons") or [])}
                if "winner_selected_probability_floor" in suppression_reasons:
                    summary.heuristic_longshots_suppressed += 1
                if "critical_market_snapshot_missing" in suppression_reasons:
                    summary.critical_context_suppressed += 1
        else:
            _record_scorer_outcome(summary, _scoring_none_reason(market))

    _enforce_prop_monotonicity(pending_recommendations, summary=summary)
    deduped_recommendations, collapsed_count, combo_suppressed = _dedupe_winner_recommendations(pending_recommendations)
    summary.inverse_winner_duplicates_collapsed = collapsed_count
    summary.combo_prop_candidates_suppressed += combo_suppressed

    db.flush()
    recommendation_market_ids: set[int] = set()
    for market, scored in deduped_recommendations:
        assert scored.recommendation is not None
        recommendation_market_ids.add(market.id)
        diagnostics = dict(scored.recommendation.scoring_diagnostics or {})
        quality_tier = str(diagnostics.get("quality_tier") or "medium")
        summary.quality_tier_counts[quality_tier] = summary.quality_tier_counts.get(quality_tier, 0) + 1
        if str(diagnostics.get("source_type") or "") == "combo_derived":
            summary.combo_prop_candidates_emitted += 1
        db.add(scored.recommendation)
        prediction = capture_prediction(
            db,
            run_id=run_id,
            event=market.event,
            market=market,
            recommendation=scored.recommendation,
            signal=scored.signal,
            metadata=scored.metadata,
            capture_scope="recommendation",
        )
        summary.recommendation_count += 1
        summary.prediction_count += 1
        parlay_candidates.append(
            ParlayCandidateInput(
                event=market.event,
                market=market,
                recommendation=scored.recommendation,
                signal=scored.signal,
                prediction=prediction,
                metadata=scored.metadata,
            )
        )
    for market, scored in current_coverage_candidates:
        if market.id in recommendation_market_ids:
            continue
        capture_prediction(
            db,
            run_id=run_id,
            event=market.event,
            market=market,
            recommendation=None,
            signal=scored.signal,
            metadata=scored.metadata,
            capture_scope="coverage",
        )
        summary.prediction_count += 1
        summary.coverage_prediction_count += 1
    db.flush()
    if capture_parlays:
        parlay_recommendation_count, parlay_prediction_count = capture_parlay_artifacts(
            db,
            run_id=run_id,
            candidates=parlay_candidates,
        )
        summary.parlay_recommendation_count = parlay_recommendation_count
        summary.parlay_prediction_count = parlay_prediction_count
    db.flush()
    return summary
