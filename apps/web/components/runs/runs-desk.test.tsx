import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RunsDesk } from "@/components/runs/runs-desk";
import type { HealthResponse, RunDetailRead, RunRead, RunSummaryCounts } from "@/lib/types";
import { renderWithProviders } from "@/test/render";

const { mockFetchHealth, mockFetchRun, mockFetchRuns } = vi.hoisted(() => ({
  mockFetchHealth: vi.fn(),
  mockFetchRun: vi.fn(),
  mockFetchRuns: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchHealth: mockFetchHealth,
    fetchRun: mockFetchRun,
    fetchRuns: mockFetchRuns,
  };
});

function summary(overrides: Partial<RunSummaryCounts> = {}): RunSummaryCounts {
  return {
    sports_records_ingested: {},
    total_kalshi_markets_seen: 0,
    supported_markets_kept: 0,
    supported_nba_props_seen: 0,
    supported_mlb_props_seen: 0,
    mapped_markets: 0,
    mapped_prop_markets: 0,
    current_slate_event_count: 0,
    current_slate_candidate_market_count: 0,
    current_slate_loaded_candidate_market_count: 0,
    current_slate_filtered_candidate_market_count: 0,
    current_slate_candidate_filter_reason_counts: {},
    current_slate_scored_market_count: 0,
    current_slate_coverage_prediction_count: 0,
    current_slate_blocking_reason: null,
    scorer_outcome_counts: {},
    recommendations_emitted: 0,
    predictions_captured: 0,
    parlay_recommendations_emitted: 0,
    parlay_predictions_captured: 0,
    prediction_settlement_updated: 0,
    parlay_prediction_settlement_updated: 0,
    prediction_outcomes: {},
    parlay_prediction_outcomes: {},
    unsupported_prop_category_counts: {},
    heuristic_longshots_suppressed: 0,
    inverse_winner_duplicates_collapsed: 0,
    combo_prop_candidates_emitted: 0,
    combo_prop_candidates_suppressed: 0,
    critical_context_suppressed: 0,
    quality_tier_counts: {},
    prop_subjects_warmed: 0,
    player_search_cache_hits: 0,
    player_search_cache_misses: 0,
    gamelog_cache_hits: 0,
    gamelog_cache_misses: 0,
    stale_gamelog_fallbacks: 0,
    combo_prop_legs_discovered: 0,
    combo_prop_legs_refreshed: 0,
    watchlist_counts_by_sport: {},
    watchlist_counts_by_prop_category: {},
    parlay_watchlist_counts_by_scope: {},
    parlay_watchlist_counts_by_leg_count: {},
    ...overrides,
  };
}

const baseHealth: HealthResponse = {
  status: "ok",
  environment: "test",
  scheduler_enabled: true,
  refresh_status: "idle",
  refresh_reason: "none",
  last_successful_refresh_at: null,
  data_stale: false,
  refresh_error_message: null,
  prop_refresh_status: "idle",
  prop_refresh_reason: "none",
  last_prop_refresh_at: null,
  prop_data_stale: false,
  prop_refresh_error_message: null,
  active_refresh_job: null,
  latest_refresh_job: null,
  active_prop_refresh_job: null,
  latest_prop_refresh_job: null,
  active_settlement_job: null,
  latest_settlement_job: null,
};

describe("RunsDesk", () => {
  it("renders settlement runs with settlement metrics and outcomes", async () => {
    const settlementSummary = summary({
      prediction_settlement_updated: 100,
      parlay_prediction_settlement_updated: 2,
      prediction_outcomes: {
        won: 38,
        lost: 61,
        push: 1,
        cancelled: 0,
        pending: 0,
        unresolved: 0,
        errors: 0,
      },
      parlay_prediction_outcomes: {
        won: 2,
        lost: 0,
        push: 0,
        cancelled: 0,
        pending: 0,
        unresolved: 0,
        errors: 0,
      },
    });
    const run: RunRead = {
      id: 1111,
      kind: "settlement",
      status: "failed",
      started_at: "2026-04-29T15:58:30.322367Z",
      finished_at: "2026-04-29T17:44:09.089985Z",
      records_processed: 102,
      error_message: "stalled - reconciled automatically",
      summary_counts: settlementSummary,
    };
    const detail: RunDetailRead = {
      ...run,
      details: {
        processed_so_far: 102,
        batch_size: 100,
      },
    };

    mockFetchHealth.mockResolvedValue(baseHealth);
    mockFetchRuns.mockResolvedValue([run]);
    mockFetchRun.mockResolvedValue(detail);

    renderWithProviders(<RunsDesk />);

    expect((await screen.findAllByText("Settlement #1111")).length).toBeGreaterThan(0);
    expect(screen.getByText("102 updates")).toBeInTheDocument();
    expect(await screen.findByText("Single Updates")).toBeInTheDocument();
    expect(screen.getByText("Parlay Updates")).toBeInTheDocument();
    expect(screen.getAllByText("won")).toHaveLength(2);
    expect(screen.getByText("lost")).toBeInTheDocument();
    expect(screen.getByText("push")).toBeInTheDocument();
    expect(screen.getByText("Parlay Outcomes")).toBeInTheDocument();
  });
});
