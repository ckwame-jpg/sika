"""Shared scoring dataclasses.

Extracted from ``scoring/__init__.py`` as part of R1 — the original
``scoring.py`` was 3,874 lines (well past the 800-line ceiling). The
types live here because they have zero downstream dependencies on
the scoring helpers, so moving them frees up the rest of the
package to import from a single canonical location.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.models import Market, Recommendation, SignalSnapshot

__all__ = [
    "ResolvedPropSubject",
    "PropResolverStats",
    "ScoredRecommendation",
    "ScoredWatchlistCapture",
    "WatchlistGenerationSummary",
]


@dataclass(slots=True)
class ResolvedPropSubject:
    sport_key: str
    athlete_id: str
    display_name: str
    team_name: str | None
    season: int
    game_logs: list[dict[str, Any]]
    player_search_cache_status: str = "miss"
    gamelog_cache_status: str = "miss"
    context_stale: bool = False
    advanced_payload: dict[str, Any] = field(default_factory=dict)
    advanced_cache_status: str = "miss"
    # Cross-source player IDs resolved during _load_advanced. Threaded
    # through here so downstream emitters (long-tail NBA, MLB lineup) can
    # look up per-player records in O(1) without re-scanning the search
    # cache. ``None`` until the resolver runs and finds a match.
    nba_stats_id: str | None = None
    mlb_stats_id: str | None = None
    # Architecture #5 — when the underlying gamelog cache row was last
    # refreshed. Feeds ``FeatureGroupSnapshot.fresh_at`` for the
    # ``nba_workload`` group so the freshness layer can compute real
    # staleness against the 24h PENALIZE TTL. ``None`` means we
    # haven't seen the cache row (network-only path or miss-without-
    # cache); the freshness check treats that as opt-out, falling
    # through to no penalty — same conservative behavior as before.
    gamelog_cached_at: datetime | None = None


@dataclass(slots=True)
class PropResolverStats:
    prop_subjects_warmed: int = 0
    player_search_cache_hits: int = 0
    player_search_cache_misses: int = 0
    gamelog_cache_hits: int = 0
    gamelog_cache_misses: int = 0
    stale_gamelog_fallbacks: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prop_subjects_warmed": self.prop_subjects_warmed,
            "player_search_cache_hits": self.player_search_cache_hits,
            "player_search_cache_misses": self.player_search_cache_misses,
            "gamelog_cache_hits": self.gamelog_cache_hits,
            "gamelog_cache_misses": self.gamelog_cache_misses,
            "stale_gamelog_fallbacks": self.stale_gamelog_fallbacks,
        }


@dataclass(slots=True)
class ScoredRecommendation:
    recommendation: Recommendation | None
    signal: SignalSnapshot
    metadata: dict[str, Any]


# Slice 6: ``_score_watchlist_markets_batch`` was previously responsible for
# both producing scored recommendations *and* persisting them via
# ``db.add(scored.signal)`` + ``capture_prediction(...)``. That made the
# scoring kernel impossible to test in isolation: any unit test that wanted
# to exercise the scoring math also got a database write. The split now
# returns a list of ``ScoredWatchlistCapture`` records describing what the
# persist step *would* do, and a separate ``_persist_scored_watchlist_captures``
# helper handles the side effects. The ``stage_*_watchlist_batch`` wrappers
# call the two in sequence so external behavior is unchanged.
@dataclass(slots=True)
class ScoredWatchlistCapture:
    market: Market
    scored: ScoredRecommendation
    capture_scope: Literal["recommendation", "coverage"] | None


@dataclass(slots=True)
class WatchlistGenerationSummary:
    recommendation_count: int = 0
    prediction_count: int = 0
    parlay_recommendation_count: int = 0
    parlay_prediction_count: int = 0
    loaded_candidate_market_count: int = 0
    filtered_candidate_market_count: int = 0
    scored_market_count: int = 0
    coverage_prediction_count: int = 0
    heuristic_longshots_suppressed: int = 0
    inverse_winner_duplicates_collapsed: int = 0
    combo_prop_candidates_emitted: int = 0
    combo_prop_candidates_suppressed: int = 0
    critical_context_suppressed: int = 0
    candidate_filter_reason_counts: dict[str, int] = field(default_factory=dict)
    outcome_reason_counts: dict[str, int] = field(default_factory=dict)
    quality_tier_counts: dict[str, int] = field(default_factory=dict)
