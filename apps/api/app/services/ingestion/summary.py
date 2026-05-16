"""Run-detail + watchlist-summary helpers.

Extracted from ``ingestion/__init__.py`` as part of R2. These three
groups of helpers all build the operator-facing ``run.details`` JSON
blob the watchlist UI reads, and live together because they're the
single source of truth for ``RefreshJob`` / ``Run`` payload shape:

- ``_prop_market_summary_counts`` / ``_parlay_watchlist_counts`` —
  DB scans that count current watchlist composition by sport / prop
  category / parlay scope.
- ``_merge_settlement_summaries`` — element-wise sum of the
  settlement-outcome counters.
- ``_build_watchlist_run_details`` — composes all of the above into
  the ``run.details`` payload + a ``records_ingested`` total.
- ``_watchlist_summary_to_payload`` / ``_watchlist_summary_from_payload`` /
  ``_merge_watchlist_summaries`` — round-trip a
  ``WatchlistGenerationSummary`` through JSON for the staged-run
  pattern (write to ``job.details`` on the producing thread, read
  back on the finalizing thread).
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Market, ParlayRecommendation, Recommendation
from app.services.ingestion.merge import _merge_count_maps
from app.services.scoring import WatchlistGenerationSummary

__all__ = [
    "_prop_market_summary_counts",
    "_parlay_watchlist_counts",
    "_merge_settlement_summaries",
    "_build_watchlist_run_details",
    "_watchlist_summary_to_payload",
    "_watchlist_summary_from_payload",
    "_merge_watchlist_summaries",
]


def _prop_market_summary_counts(db: Session) -> tuple[int, dict[str, int], dict[str, int]]:
    watchlist_by_sport: dict[str, int] = {}
    watchlist_by_prop_category: dict[str, int] = {}

    recommendations = db.scalars(select(Recommendation).join(Market, Recommendation.market_id == Market.id)).all()
    for recommendation in recommendations:
        market = db.scalar(select(Market).where(Market.id == recommendation.market_id))
        if not market:
            continue
        sport_key = market.sport_key or "UNKNOWN"
        watchlist_by_sport[sport_key] = watchlist_by_sport.get(sport_key, 0) + 1
        raw_data = market.raw_data or {}
        if raw_data.get("copilot_market_family") == "player_prop":
            stat_key = str(raw_data.get("copilot_stat_key") or "unknown")
            watchlist_by_prop_category[stat_key] = watchlist_by_prop_category.get(stat_key, 0) + 1

    mapped_prop_markets = 0
    prop_markets = db.scalars(select(Market).where(Market.raw_data.is_not(None))).all()
    for market in prop_markets:
        raw_data = market.raw_data or {}
        if raw_data.get("copilot_market_family") != "player_prop":
            continue
        if market.event_id:
            mapped_prop_markets += 1

    return mapped_prop_markets, watchlist_by_sport, watchlist_by_prop_category


def _parlay_watchlist_counts(db: Session) -> tuple[dict[str, int], dict[str, int]]:
    parlay_watchlist_by_scope: dict[str, int] = {}
    parlay_watchlist_by_leg_count: dict[str, int] = {}
    parlay_recommendations = db.scalars(select(ParlayRecommendation)).all()
    for parlay in parlay_recommendations:
        parlay_watchlist_by_scope[parlay.sport_scope] = parlay_watchlist_by_scope.get(parlay.sport_scope, 0) + 1
        leg_key = str(parlay.leg_count)
        parlay_watchlist_by_leg_count[leg_key] = parlay_watchlist_by_leg_count.get(leg_key, 0) + 1
    return parlay_watchlist_by_scope, parlay_watchlist_by_leg_count


def _merge_settlement_summaries(*summaries: dict[str, int]) -> dict[str, int]:
    merged = {
        "processed": 0,
        "updated": 0,
        "won": 0,
        "lost": 0,
        "push": 0,
        "cancelled": 0,
        "pending": 0,
        "unresolved": 0,
        "errors": 0,
    }
    for summary in summaries:
        for key in merged:
            merged[key] += int(summary.get(key) or 0)
    return merged


def _build_watchlist_run_details(
    db: Session,
    *,
    sports: Iterable[str] | None,
    sports_summary: dict[str, object] | None,
    kalshi_summary: dict[str, object],
    mapped_count: int,
    watchlist_summary,
    shadow_prediction_count: int = 0,
    shadow_parlay_prediction_count: int = 0,
    single_settlement_summary: dict[str, int] | None = None,
    parlay_settlement_summary: dict[str, int] | None = None,
    extra_details: dict[str, object] | None = None,
) -> tuple[dict[str, object], int]:
    single_settlement_summary = single_settlement_summary or {
        "updated": 0,
        "won": 0,
        "lost": 0,
        "push": 0,
        "cancelled": 0,
        "pending": 0,
        "unresolved": 0,
        "errors": 0,
    }
    parlay_settlement_summary = parlay_settlement_summary or {
        "updated": 0,
        "won": 0,
        "lost": 0,
        "push": 0,
        "cancelled": 0,
        "pending": 0,
        "unresolved": 0,
        "errors": 0,
    }
    mapped_prop_markets, watchlist_by_sport, watchlist_by_prop_category = _prop_market_summary_counts(db)
    parlay_watchlist_by_scope, parlay_watchlist_by_leg_count = _parlay_watchlist_counts(db)
    records = (
        int((sports_summary or {}).get("processed") or 0)
        + int(kalshi_summary.get("processed") or 0)
        + mapped_count
        + watchlist_summary.recommendation_count
        + watchlist_summary.prediction_count
        + watchlist_summary.parlay_recommendation_count
        + watchlist_summary.parlay_prediction_count
        + shadow_prediction_count
        + shadow_parlay_prediction_count
        + int(single_settlement_summary.get("updated") or 0)
        + int(parlay_settlement_summary.get("updated") or 0)
    )
    details: dict[str, object] = {
        "sports_requested": list(sports or get_settings().enabled_sports),
        "sports_records_ingested": (sports_summary or {}).get("sports_records_ingested") or {},
        "sports_fetch_errors": (sports_summary or {}).get("sports_fetch_errors") or {},
        "total_kalshi_markets_seen": kalshi_summary.get("total_kalshi_markets_seen") or 0,
        "supported_markets_kept": kalshi_summary.get("processed") or 0,
        "market_snapshots_written": kalshi_summary.get("market_snapshots_written") or 0,
        "supported_nba_props_seen": kalshi_summary.get("supported_nba_props_seen") or 0,
        "supported_mlb_props_seen": kalshi_summary.get("supported_mlb_props_seen") or 0,
        "unsupported_prop_category_counts": kalshi_summary.get("unsupported_prop_category_counts") or {},
        "combo_prop_legs_discovered": kalshi_summary.get("combo_prop_legs_discovered") or 0,
        "combo_prop_legs_refreshed": kalshi_summary.get("combo_prop_legs_refreshed") or 0,
        "mapped_markets": mapped_count,
        "mapped_prop_markets": mapped_prop_markets,
        "current_slate_event_count": int((extra_details or {}).get("current_slate_event_count") or 0),
        "current_slate_candidate_market_count": int((extra_details or {}).get("current_slate_candidate_market_count") or 0),
        "current_slate_loaded_candidate_market_count": watchlist_summary.loaded_candidate_market_count,
        "current_slate_filtered_candidate_market_count": watchlist_summary.filtered_candidate_market_count,
        "current_slate_candidate_filter_reason_counts": dict(watchlist_summary.candidate_filter_reason_counts or {}),
        "current_slate_scored_market_count": watchlist_summary.scored_market_count,
        "current_slate_coverage_prediction_count": watchlist_summary.coverage_prediction_count,
        "current_slate_blocking_reason": (extra_details or {}).get("current_slate_blocking_reason"),
        "scorer_outcome_counts": dict(watchlist_summary.outcome_reason_counts or {}),
        "recommendations_emitted": watchlist_summary.recommendation_count,
        "predictions_captured": watchlist_summary.prediction_count,
        "parlay_recommendations_emitted": watchlist_summary.parlay_recommendation_count,
        "parlay_predictions_captured": watchlist_summary.parlay_prediction_count,
        "heuristic_longshots_suppressed": watchlist_summary.heuristic_longshots_suppressed,
        "inverse_winner_duplicates_collapsed": watchlist_summary.inverse_winner_duplicates_collapsed,
        "combo_prop_candidates_emitted": watchlist_summary.combo_prop_candidates_emitted,
        "combo_prop_candidates_suppressed": watchlist_summary.combo_prop_candidates_suppressed,
        "critical_context_suppressed": watchlist_summary.critical_context_suppressed,
        "quality_tier_counts": watchlist_summary.quality_tier_counts,
        "shadow_predictions_captured": shadow_prediction_count,
        "shadow_parlay_predictions_captured": shadow_parlay_prediction_count,
        "prediction_settlement_updated": int(single_settlement_summary.get("updated") or 0),
        "parlay_prediction_settlement_updated": int(parlay_settlement_summary.get("updated") or 0),
        "prediction_outcomes": {
            "won": int(single_settlement_summary.get("won") or 0),
            "lost": int(single_settlement_summary.get("lost") or 0),
            "push": int(single_settlement_summary.get("push") or 0),
            "cancelled": int(single_settlement_summary.get("cancelled") or 0),
            "pending": int(single_settlement_summary.get("pending") or 0),
            "unresolved": int(single_settlement_summary.get("unresolved") or 0),
            "errors": int(single_settlement_summary.get("errors") or 0),
        },
        "parlay_prediction_outcomes": {
            "won": int(parlay_settlement_summary.get("won") or 0),
            "lost": int(parlay_settlement_summary.get("lost") or 0),
            "push": int(parlay_settlement_summary.get("push") or 0),
            "cancelled": int(parlay_settlement_summary.get("cancelled") or 0),
            "pending": int(parlay_settlement_summary.get("pending") or 0),
            "unresolved": int(parlay_settlement_summary.get("unresolved") or 0),
            "errors": int(parlay_settlement_summary.get("errors") or 0),
        },
        "watchlist_counts_by_sport": watchlist_by_sport,
        "watchlist_counts_by_prop_category": watchlist_by_prop_category,
        "parlay_watchlist_counts_by_scope": parlay_watchlist_by_scope,
        "parlay_watchlist_counts_by_leg_count": parlay_watchlist_by_leg_count,
    }
    if extra_details:
        details.update(extra_details)
    return details, records


def _watchlist_summary_to_payload(summary: WatchlistGenerationSummary) -> dict[str, object]:
    return {
        "recommendation_count": summary.recommendation_count,
        "prediction_count": summary.prediction_count,
        "parlay_recommendation_count": summary.parlay_recommendation_count,
        "parlay_prediction_count": summary.parlay_prediction_count,
        "loaded_candidate_market_count": summary.loaded_candidate_market_count,
        "filtered_candidate_market_count": summary.filtered_candidate_market_count,
        "scored_market_count": summary.scored_market_count,
        "coverage_prediction_count": summary.coverage_prediction_count,
        "heuristic_longshots_suppressed": summary.heuristic_longshots_suppressed,
        "inverse_winner_duplicates_collapsed": summary.inverse_winner_duplicates_collapsed,
        "combo_prop_candidates_emitted": summary.combo_prop_candidates_emitted,
        "combo_prop_candidates_suppressed": summary.combo_prop_candidates_suppressed,
        "critical_context_suppressed": summary.critical_context_suppressed,
        "candidate_filter_reason_counts": dict(summary.candidate_filter_reason_counts or {}),
        "outcome_reason_counts": dict(summary.outcome_reason_counts or {}),
        "quality_tier_counts": dict(summary.quality_tier_counts or {}),
    }


def _watchlist_summary_from_payload(payload: dict[str, object] | None) -> WatchlistGenerationSummary:
    payload = dict(payload or {})
    return WatchlistGenerationSummary(
        recommendation_count=int(payload.get("recommendation_count") or 0),
        prediction_count=int(payload.get("prediction_count") or 0),
        parlay_recommendation_count=int(payload.get("parlay_recommendation_count") or 0),
        parlay_prediction_count=int(payload.get("parlay_prediction_count") or 0),
        loaded_candidate_market_count=int(payload.get("loaded_candidate_market_count") or 0),
        filtered_candidate_market_count=int(payload.get("filtered_candidate_market_count") or 0),
        scored_market_count=int(payload.get("scored_market_count") or 0),
        coverage_prediction_count=int(payload.get("coverage_prediction_count") or 0),
        heuristic_longshots_suppressed=int(payload.get("heuristic_longshots_suppressed") or 0),
        inverse_winner_duplicates_collapsed=int(payload.get("inverse_winner_duplicates_collapsed") or 0),
        combo_prop_candidates_emitted=int(payload.get("combo_prop_candidates_emitted") or 0),
        combo_prop_candidates_suppressed=int(payload.get("combo_prop_candidates_suppressed") or 0),
        critical_context_suppressed=int(payload.get("critical_context_suppressed") or 0),
        candidate_filter_reason_counts={str(key): int(value or 0) for key, value in dict(payload.get("candidate_filter_reason_counts") or {}).items()},
        outcome_reason_counts={str(key): int(value or 0) for key, value in dict(payload.get("outcome_reason_counts") or {}).items()},
        quality_tier_counts={str(key): int(value or 0) for key, value in dict(payload.get("quality_tier_counts") or {}).items()},
    )


def _merge_watchlist_summaries(
    left: WatchlistGenerationSummary,
    right: WatchlistGenerationSummary,
) -> WatchlistGenerationSummary:
    merged = WatchlistGenerationSummary(
        recommendation_count=left.recommendation_count + right.recommendation_count,
        prediction_count=left.prediction_count + right.prediction_count,
        parlay_recommendation_count=left.parlay_recommendation_count + right.parlay_recommendation_count,
        parlay_prediction_count=left.parlay_prediction_count + right.parlay_prediction_count,
        loaded_candidate_market_count=left.loaded_candidate_market_count + right.loaded_candidate_market_count,
        filtered_candidate_market_count=left.filtered_candidate_market_count + right.filtered_candidate_market_count,
        scored_market_count=left.scored_market_count + right.scored_market_count,
        coverage_prediction_count=left.coverage_prediction_count + right.coverage_prediction_count,
        heuristic_longshots_suppressed=left.heuristic_longshots_suppressed + right.heuristic_longshots_suppressed,
        inverse_winner_duplicates_collapsed=left.inverse_winner_duplicates_collapsed + right.inverse_winner_duplicates_collapsed,
        combo_prop_candidates_emitted=left.combo_prop_candidates_emitted + right.combo_prop_candidates_emitted,
        combo_prop_candidates_suppressed=left.combo_prop_candidates_suppressed + right.combo_prop_candidates_suppressed,
        critical_context_suppressed=left.critical_context_suppressed + right.critical_context_suppressed,
        candidate_filter_reason_counts=_merge_count_maps(left.candidate_filter_reason_counts, right.candidate_filter_reason_counts),
        outcome_reason_counts=_merge_count_maps(left.outcome_reason_counts, right.outcome_reason_counts),
        quality_tier_counts=_merge_count_maps(left.quality_tier_counts, right.quality_tier_counts),
    )
    return merged
