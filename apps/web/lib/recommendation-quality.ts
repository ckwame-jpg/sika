import type { PredictionRead, RecommendationRead } from "@/lib/types";
import type { RecommendationViewMode } from "@/components/filters/quality-filter-select";

type QualityCandidate = Pick<
  RecommendationRead,
  "quality_tier" | "selected_side_probability" | "context_coverage_score" | "market_family" | "source_type"
> | Pick<
  PredictionRead,
  "quality_tier" | "selected_side_probability" | "context_coverage_score" | "market_family" | "source_type"
>;

export function matchesRecommendationViewMode(
  item: QualityCandidate,
  mode: RecommendationViewMode,
): boolean {
  if (mode === "balanced") {
    return true;
  }
  const qualityTier = (item.quality_tier || "").toLowerCase();
  const marketFamily = (item.market_family || "").toLowerCase();
  const selectedProbability = item.selected_side_probability ?? 0;
  const contextCoverage = item.context_coverage_score ?? 0;
  const sourceType = (item.source_type || "").toLowerCase();

  if (qualityTier !== "high") {
    return false;
  }
  if (sourceType === "combo_derived" && contextCoverage < 0.82) {
    return false;
  }
  if (marketFamily === "winner") {
    return selectedProbability >= 0.5 && contextCoverage >= 0.78;
  }
  if (marketFamily === "player_prop") {
    return selectedProbability >= 0.55 && contextCoverage >= 0.8;
  }
  return selectedProbability >= 0.5 && contextCoverage >= 0.78;
}
