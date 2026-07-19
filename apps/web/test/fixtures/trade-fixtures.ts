import type {
  HealthResponse,
  TradeDeskPlayerProp,
  TradeDeskResponse,
  TradeDeskThreshold,
} from "@/lib/types";

function threshold(overrides: Partial<TradeDeskThreshold>): TradeDeskThreshold {
  return {
    ticker: overrides.ticker ?? "fixture-threshold",
    threshold: overrides.threshold ?? 10,
    probability_yes: overrides.probability_yes ?? 0.5,
    selected_side: overrides.selected_side ?? "yes",
    selected_side_probability: overrides.selected_side_probability ?? overrides.probability_yes ?? 0.5,
    entry_price: overrides.entry_price ?? 0.4,
    edge: overrides.edge ?? 0.1,
    confidence: overrides.confidence ?? 0.7,
    is_best: overrides.is_best ?? false,
    kalshi_url: overrides.kalshi_url ?? null,
    time_to_close_minutes: overrides.time_to_close_minutes ?? null,
    // Smarter #21 phase 2d — default to null (no trained sidecar);
    // tests that want to exercise the band can override.
    prediction_interval: overrides.prediction_interval ?? null,
    // Smarter #22 PR A — default to empty / null (no stale groups);
    // tests that want to exercise the freshness badge override.
    freshness_stale_groups: overrides.freshness_stale_groups ?? [],
    freshness_confidence_delta: overrides.freshness_confidence_delta ?? null,
  };
}

export const davionMitchellProp: TradeDeskPlayerProp = {
  subject_name: "Davion Mitchell",
  subject_team: "TOR",
  stat_groups: [
    {
      stat_key: "points",
      thresholds: [
        threshold({
          ticker: "KXNBAPTS-DAVION-10",
          threshold: 10,
          probability_yes: 0.721,
          selected_side_probability: 0.721,
          entry_price: 0.40,
          edge: 0.321,
          confidence: 0.76,
          is_best: true,
          kalshi_url: "https://kalshi.com/markets/davion-10",
        }),
        threshold({
          ticker: "KXNBAPTS-DAVION-15",
          threshold: 15,
          probability_yes: 0.528,
          selected_side_probability: 0.528,
          entry_price: 0.43,
          edge: 0.098,
          confidence: 0.68,
          kalshi_url: "https://kalshi.com/markets/davion-15",
        }),
      ],
    },
    {
      stat_key: "assists",
      thresholds: [
        threshold({
          ticker: "KXNBAAST-DAVION-4",
          threshold: 4,
          probability_yes: 0.894,
          selected_side_probability: 0.894,
          entry_price: 0.85,
          edge: 0.044,
          confidence: 0.62,
          is_best: true,
          kalshi_url: "https://kalshi.com/markets/davion-4-assists",
        }),
        threshold({
          ticker: "KXNBAAST-DAVION-6",
          threshold: 6,
          probability_yes: 0.671,
          selected_side_probability: 0.671,
          entry_price: 0.62,
          edge: 0.051,
          confidence: 0.58,
          kalshi_url: "https://kalshi.com/markets/davion-6-assists",
        }),
      ],
    },
  ],
  best_edge: 0.321,
  best_win_prob: 0.894,
};

export const tradeDeskFixture: TradeDeskResponse = {
  events: [
    {
      event_id: 1,
      event_name: "Miami Heat at Toronto Raptors",
      event_status: "scheduled",
      starts_at: "2026-04-07T23:00:00Z",
      sport_key: "NBA",
      candidate_market_count: 7,
      scored_market_count: 7,
      coverage_prediction_count: 0,
      game_lines: [
        {
          ticker: "KXNBAGAME-RAPTORS",
          market_title: "Miami Heat at Toronto Raptors Winner?",
          display_label: "Toronto Raptors to win",
          sport_key: "NBA",
          market_kind: "game_winner",
          selected_side: "yes",
          projected_side_label: "Toronto Raptors",
          selected_side_probability: 0.61,
          entry_price: 0.53,
          edge: 0.08,
          confidence: 0.74,
          kalshi_url: "https://kalshi.com/markets/raptors-win",
          numeric_line: null,
      total_direction: null,
          price_history: [],
          time_to_close_minutes: 269,
          freshness_stale_groups: [],
          freshness_confidence_delta: null,
        },
        {
          ticker: "KXNBASPREAD-RAPTORS-3_5",
          market_title: "Miami Heat at Toronto Raptors Spread?",
          display_label: "Toronto Raptors -3.5",
          sport_key: "NBA",
          market_kind: "spread",
          selected_side: "yes",
          projected_side_label: "Toronto Raptors -3.5",
          selected_side_probability: 0.58,
          entry_price: 0.49,
          edge: 0.09,
          confidence: 0.69,
          kalshi_url: "https://kalshi.com/markets/raptors-spread",
          numeric_line: -3.5,
      total_direction: null,
          price_history: [],
          time_to_close_minutes: 322,
          freshness_stale_groups: [],
          freshness_confidence_delta: null,
        },
        {
          ticker: "KXNBATOTAL-219_5",
          market_title: "Miami Heat at Toronto Raptors Total?",
          display_label: "Over 219.5",
          sport_key: "NBA",
          market_kind: "total",
          selected_side: "yes",
          projected_side_label: "Over 219.5",
          selected_side_probability: 0.57,
          entry_price: 0.47,
          edge: 0.10,
          confidence: 0.65,
          kalshi_url: "https://kalshi.com/markets/raptors-total",
          numeric_line: 219.5,
      total_direction: null,
          price_history: [],
          time_to_close_minutes: 154,
          freshness_stale_groups: [],
          freshness_confidence_delta: null,
        },
      ],
      player_props: [davionMitchellProp],
    },
  ],
  research_sports: [
    {
      sport_key: "NFL",
      availability_mode: "research_only",
      events_count: 2,
      recommendations_count: 5,
      last_refresh_at: "2026-04-07T18:00:00Z",
    },
  ],
  generated_at: "2026-04-07T18:00:00Z",
  freshness_status: "fresh",
  event_count: 1,
  candidate_market_count: 7,
  scored_market_count: 7,
  recommendation_count: 7,
  coverage_prediction_count: 0,
  blocking_reason: null,
  generated_from_run_id: 99,
  previous_slate: null,
};

export const tradeDeskFixtureWithNonMonotonicGroup: TradeDeskResponse = {
  ...tradeDeskFixture,
  events: [
    {
      ...tradeDeskFixture.events[0],
      player_props: [
        davionMitchellProp,
        {
          subject_name: "Bam Adebayo",
          subject_team: "MIA",
          stat_groups: [
            {
              stat_key: "rebounds",
              thresholds: [
                threshold({
                  ticker: "KXNBAREB-BAM-8",
                  threshold: 8,
                  probability_yes: 0.61,
                  selected_side_probability: 0.61,
                  entry_price: 0.55,
                  edge: 0.06,
                  confidence: 0.62,
                  is_best: true,
                }),
                threshold({
                  ticker: "KXNBAREB-BAM-10",
                  threshold: 10,
                  probability_yes: 0.82,
                  selected_side_probability: 0.82,
                  entry_price: 0.76,
                  edge: 0.06,
                  confidence: 0.61,
                }),
              ],
            },
          ],
          best_edge: 0.06,
          best_win_prob: 0.82,
        },
      ],
    },
  ],
};

export const healthFixture: HealthResponse = {
  status: "ok",
  environment: "test",
  scheduler_enabled: true,
  refresh_status: "idle",
  refresh_reason: "none",
  last_successful_refresh_at: "2026-04-07T18:00:00Z",
  data_stale: false,
  refresh_error_message: null,
  prop_refresh_status: "idle",
  prop_refresh_reason: "none",
  last_prop_refresh_at: "2026-04-07T18:00:00Z",
  prop_data_stale: false,
  prop_refresh_error_message: null,
  active_refresh_job: null,
  latest_refresh_job: null,
  active_prop_refresh_job: null,
  latest_prop_refresh_job: null,
  active_settlement_job: null,
  latest_settlement_job: null,
  // Bug #40 migration surfaced this — Smarter #23 added upstream_sources
  // to HealthResponse but the hand-written mirror never picked it up.
  upstream_sources: [],
};
