"""Monotonicity clamps + thesis-key dedupe for scored
recommendations and predictions.

Extracted from ``scoring/__init__.py`` as part of R1. The two
concepts are paired:

- ``_enforce_prop_monotonicity`` / ``_apply_prediction_monotonicity``
  clamp later-threshold prop probabilities to never exceed the
  preceding threshold's probability (a 30+ points line can't be more
  likely than a 20+ points line on the same player). When the clamp
  pushes edge below the watchlist floor, the recommendation is
  suppressed (bug #9).
- ``_dedupe_winner_recommendations`` / ``_dedupe_prediction_recommendations``
  collapse duplicate winner predictions that share a
  ``selected_thesis_key`` to a single best representative ranked by
  ``(selection_score, edge, -suggested_price, quality_tier_rank)``.

Both groups share ``_quality_tier_rank`` (the ordering helper) and
the ``WatchlistGenerationSummary`` accounting hooks, so they live
together.
"""

from __future__ import annotations

from collections import defaultdict

from app.config import get_settings
from app.models import Market, Prediction, Recommendation
from app.services.model_families import single_family_key, watchlist_min_edge_for
from app.services.scoring.types import (
    ScoredRecommendation,
    WatchlistGenerationSummary,
)

__all__ = [
    "_quality_tier_rank",
    "_enforce_prop_monotonicity",
    "_dedupe_winner_recommendations",
    "_prediction_recommendation_tuple",
    "_apply_prediction_monotonicity",
    "_dedupe_prediction_recommendations",
]


def _quality_tier_rank(value: str | None) -> int:
    return {"high": 2, "medium": 1, "low": 0}.get((value or "").lower(), -1)


def _enforce_prop_monotonicity(
    scored_recommendations: list[tuple[Market, ScoredRecommendation]],
    *,
    summary: WatchlistGenerationSummary | None = None,
) -> None:
    settings = get_settings()
    grouped: dict[tuple[int, str, str], list[tuple[Market, ScoredRecommendation]]] = defaultdict(list)
    for market, scored in scored_recommendations:
        metadata = scored.metadata or {}
        if str(metadata.get("copilot_market_family") or "") != "player_prop":
            continue
        subject_name = str(metadata.get("copilot_subject_name") or "").strip()
        stat_key = str(metadata.get("copilot_stat_key") or "").strip()
        threshold = metadata.get("copilot_threshold")
        if not subject_name or not stat_key or threshold is None or market.event_id is None:
            continue
        grouped[(market.event_id, subject_name.lower(), stat_key)].append((market, scored))

    for group in grouped.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda item: float((item[1].metadata or {}).get("copilot_threshold") or 0.0))
        for index in range(1, len(group)):
            previous_scored = group[index - 1][1]
            current_scored = group[index][1]
            previous_probability = float(previous_scored.signal.fair_yes_price or 0.0)
            current_probability = float(current_scored.signal.fair_yes_price or 0.0)
            if current_probability <= previous_probability:
                continue

            clamped_probability = round(previous_probability, 4)
            current_scored.signal.fair_yes_price = clamped_probability
            current_scored.signal.fair_no_price = round(1 - clamped_probability, 4)

            signal_diagnostics = dict(current_scored.signal.scoring_diagnostics or {})
            signal_diagnostics["monotonicity_adjusted"] = True
            current_scored.signal.scoring_diagnostics = signal_diagnostics

            recommendation = current_scored.recommendation
            if recommendation is None:
                continue

            recommendation.edge = round(clamped_probability - recommendation.suggested_price, 4)
            # Codex PR #33 P2/P3: mirror the recomputed edge AND the clamped
            # selected_side_probability onto the signal so coverage captures
            # (which read signal fields when recommendation is None) persist
            # the post-clamp values, not the stale pre-clamp pair.
            current_scored.signal.edge = recommendation.edge
            signal_diagnostics = dict(current_scored.signal.scoring_diagnostics or {})
            signal_diagnostics["selected_side_probability"] = clamped_probability
            signal_diagnostics["monotonicity_adjusted"] = True
            current_scored.signal.scoring_diagnostics = signal_diagnostics
            recommendation.scoring_diagnostics = {
                **dict(recommendation.scoring_diagnostics or {}),
                "selected_side_probability": clamped_probability,
                "monotonicity_adjusted": True,
            }

            # Bug #9: when the clamp drops edge below the watchlist floor,
            # the user shouldn't see this pick — it would have been filtered
            # out had the lowered probability been the original. Record the
            # suppression reason on the signal (so ops can explain it) and
            # clear the recommendation so downstream surfaces drop it.
            #
            # Smarter #30 — consult the per-family floor (empty registry
            # today resolves to ``settings.watchlist_min_edge`` for every
            # family). The scope here is always ``player_prop`` per the
            # outer filter, so ``single_family_key`` resolves to
            # ``nba_props`` / ``mlb_props``.
            family_key = single_family_key(market.sport_key, "player_prop")
            family_min_edge = watchlist_min_edge_for(family_key, settings.watchlist_min_edge)
            if recommendation.edge < family_min_edge:
                signal_diagnostics = dict(current_scored.signal.scoring_diagnostics or {})
                signal_diagnostics["monotonicity_edge_below_min"] = True
                suppression_reasons = list(signal_diagnostics.get("suppression_reasons") or [])
                if "monotonicity_edge_below_min" not in suppression_reasons:
                    suppression_reasons.append("monotonicity_edge_below_min")
                signal_diagnostics["suppression_reasons"] = suppression_reasons
                current_scored.signal.scoring_diagnostics = signal_diagnostics
                current_scored.recommendation = None
                # Codex PR #33 round-3 P3: the market was counted as
                # ``recommended`` upstream before monotonicity ran. Reclassify
                # it so scorer-outcome metrics surface this suppression
                # instead of overreporting recommendations.
                if summary is not None:
                    counts = summary.outcome_reason_counts
                    if counts.get("recommended", 0) > 0:
                        counts["recommended"] = counts["recommended"] - 1
                        if counts["recommended"] == 0:
                            counts.pop("recommended", None)
                    counts["suppressed_monotonicity_edge_below_min"] = (
                        counts.get("suppressed_monotonicity_edge_below_min", 0) + 1
                    )


def _dedupe_winner_recommendations(
    scored_recommendations: list[tuple[Market, ScoredRecommendation]],
) -> tuple[list[tuple[Market, ScoredRecommendation]], int, int]:
    deduped: dict[str, tuple[Market, ScoredRecommendation]] = {}
    passthrough: list[tuple[Market, ScoredRecommendation]] = []
    collapsed_count = 0
    combo_suppressed = 0

    for market, scored in scored_recommendations:
        recommendation = scored.recommendation
        if recommendation is None:
            continue
        diagnostics = dict(recommendation.scoring_diagnostics or {})
        thesis_key = diagnostics.get("selected_thesis_key")
        if not thesis_key:
            passthrough.append((market, scored))
            continue

        current = deduped.get(str(thesis_key))
        if current is None:
            deduped[str(thesis_key)] = (market, scored)
            continue

        _, existing_scored = current
        existing_recommendation = existing_scored.recommendation
        assert existing_recommendation is not None

        candidate_tuple = (
            recommendation.selection_score or 0.0,
            recommendation.edge,
            -recommendation.suggested_price,
            _quality_tier_rank(diagnostics.get("quality_tier")),
        )
        existing_tuple = (
            existing_recommendation.selection_score or 0.0,
            existing_recommendation.edge,
            -existing_recommendation.suggested_price,
            _quality_tier_rank((existing_recommendation.scoring_diagnostics or {}).get("quality_tier")),
        )
        if candidate_tuple > existing_tuple:
            if str((existing_recommendation.scoring_diagnostics or {}).get("source_type") or "") == "combo_derived":
                combo_suppressed += 1
            deduped[str(thesis_key)] = (market, scored)
        else:
            if str(diagnostics.get("source_type") or "") == "combo_derived":
                combo_suppressed += 1
        collapsed_count += 1

    return [*passthrough, *deduped.values()], collapsed_count, combo_suppressed


def _prediction_recommendation_tuple(prediction: Prediction) -> tuple[float, float, float, int]:
    diagnostics = dict(prediction.scoring_diagnostics or {})
    return (
        prediction.selection_score or 0.0,
        prediction.edge,
        -prediction.suggested_price,
        _quality_tier_rank(diagnostics.get("quality_tier")),
    )


def _apply_prediction_monotonicity(
    predictions: list[Prediction],
    *,
    summary: WatchlistGenerationSummary | None = None,
) -> None:
    settings = get_settings()
    grouped: dict[tuple[int, str, str], list[Prediction]] = defaultdict(list)
    for prediction in predictions:
        if prediction.market_family != "player_prop":
            continue
        subject_name = str(prediction.subject_name or "").strip()
        stat_key = str(prediction.stat_key or "").strip()
        threshold = prediction.threshold
        if not subject_name or not stat_key or threshold is None or prediction.event_id is None:
            continue
        grouped[(prediction.event_id, subject_name.lower(), stat_key)].append(prediction)

    for group in grouped.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda prediction: float(prediction.threshold or 0.0))
        for index in range(1, len(group)):
            previous = group[index - 1]
            current = group[index]
            previous_probability = float(previous.fair_yes_price or 0.0)
            current_probability = float(current.fair_yes_price or 0.0)
            if current_probability <= previous_probability:
                continue
            clamped_probability = round(previous_probability, 4)
            current.fair_yes_price = clamped_probability
            current.fair_no_price = round(1 - clamped_probability, 4)
            current.edge = round(clamped_probability - current.suggested_price, 4)
            diagnostics = dict(current.scoring_diagnostics or {})
            diagnostics["selected_side_probability"] = clamped_probability
            diagnostics["monotonicity_adjusted"] = True
            # Smarter #30 — per-family floor (same mechanism as the
            # recommendation path above). ``Prediction.market_family``
            # is always ``"player_prop"`` here per the outer filter.
            family_key = single_family_key(current.sport_key, "player_prop")
            family_min_edge = watchlist_min_edge_for(family_key, settings.watchlist_min_edge)
            if current.edge < family_min_edge:
                # Bug #9: the clamp dropped edge below the watchlist floor —
                # mark the prediction suppressed so finalize_staged_watchlist
                # excludes it from the dedup pass (and the operator never
                # sees it on the watchlist). capture_scope == "suppressed"
                # is the same filter the downstream pipeline already uses.
                diagnostics["monotonicity_edge_below_min"] = True
                suppression_reasons = list(diagnostics.get("suppression_reasons") or [])
                if "monotonicity_edge_below_min" not in suppression_reasons:
                    suppression_reasons.append("monotonicity_edge_below_min")
                diagnostics["suppression_reasons"] = suppression_reasons
                current.capture_scope = "suppressed"
                # Codex PR #33 round-4 P2: the staged path counted this
                # prediction as ``recommended`` during batch scoring; keep
                # the run summary aligned with the predictions finalize
                # actually emits.
                if summary is not None:
                    counts = summary.outcome_reason_counts
                    if counts.get("recommended", 0) > 0:
                        counts["recommended"] = counts["recommended"] - 1
                        if counts["recommended"] == 0:
                            counts.pop("recommended", None)
                    counts["suppressed_monotonicity_edge_below_min"] = (
                        counts.get("suppressed_monotonicity_edge_below_min", 0) + 1
                    )
            current.scoring_diagnostics = diagnostics


def _dedupe_prediction_recommendations(
    predictions: list[Prediction],
) -> tuple[list[Prediction], int, int]:
    deduped: dict[str, Prediction] = {}
    passthrough: list[Prediction] = []
    collapsed_count = 0
    combo_suppressed = 0

    for prediction in predictions:
        diagnostics = dict(prediction.scoring_diagnostics or {})
        thesis_key = diagnostics.get("selected_thesis_key")
        if not thesis_key:
            passthrough.append(prediction)
            continue

        current = deduped.get(str(thesis_key))
        if current is None:
            deduped[str(thesis_key)] = prediction
            continue

        if _prediction_recommendation_tuple(prediction) > _prediction_recommendation_tuple(current):
            if str((current.scoring_diagnostics or {}).get("source_type") or "") == "combo_derived":
                combo_suppressed += 1
            deduped[str(thesis_key)] = prediction
        else:
            if str(diagnostics.get("source_type") or "") == "combo_derived":
                combo_suppressed += 1
        collapsed_count += 1

    return [*passthrough, *deduped.values()], collapsed_count, combo_suppressed
