import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TradeTicket, type TradeSelection } from "@/components/trade/trade-ticket";
import { renderWithProviders } from "@/test/render";

vi.mock("@/components/positions/trade-dialog", () => ({
  TradeDialog: () => null,
}));

const selection: TradeSelection = {
  kind: "player_prop",
  ticker: "KXNBAPTS-DAVION-10",
  eventId: 1,
  marketTitle: "Davion Mitchell: 10+ points",
  eventName: "Miami Heat at Toronto Raptors",
  sportKey: "NBA",
  marketKind: "player_prop",
  displayLabel: "Davion Mitchell 10+ points",
  projectedSideLabel: null,
  selectedSide: "yes",
  selectedSideProbability: 0.721,
  entryPrice: 0.4,
  edge: 0.321,
  confidence: 0.76,
  kalshiUrl: "https://kalshi.com/markets/davion-10",
  subjectName: "Davion Mitchell",
  subjectTeam: "TOR",
  statKey: "points",
  threshold: 10,
};

describe("TradeTicket", () => {
  it("renders the selected market without any portfolio exposure cards", () => {
    renderWithProviders(<TradeTicket selection={selection} />);

    expect(screen.getByTestId("trade-ticket-title")).toHaveTextContent("Davion Mitchell 10+ points");
    expect(screen.getByText("Paper trade")).toBeInTheDocument();
    expect(screen.queryByText("Your Exposure")).not.toBeInTheDocument();
    expect(screen.queryByText("Event Context")).not.toBeInTheDocument();
  });

  it("does NOT render the prediction-interval band when the selection lacks one", () => {
    // Smarter #21 phase 2d — the band is purely additive and must
    // not appear for stat keys without a trained sidecar (the
    // common case until more coverage migrates into the ok band).
    renderWithProviders(<TradeTicket selection={selection} />);

    expect(screen.queryByRole("group", { name: /prediction interval/i })).not.toBeInTheDocument();
  });

  it("renders the prediction-interval band when the selection carries a diagnostic", () => {
    renderWithProviders(
      <TradeTicket
        selection={{
          ...selection,
          predictionInterval: {
            p10: 8.4,
            p50: 11.2,
            p90: 14.1,
            threshold: 10,
            source: "interval_model_v1",
            coverage_status: "ok",
            yes_probability_from_interval: 0.78,
            yes_probability_from_poisson: 0.72,
            delta: 0.06,
          },
        }}
      />,
    );

    expect(screen.getByRole("group", { name: /prediction interval/i })).toBeInTheDocument();
  });

  it("does NOT render the freshness badge when the selection has no stale groups", () => {
    // Smarter #22 PR A — like the interval band, the badge must not
    // appear for picks where all features are fresh (the common case).
    renderWithProviders(<TradeTicket selection={selection} />);

    expect(screen.queryByRole("group", { name: /stale feature/i })).not.toBeInTheDocument();
  });

  it("renders the freshness badge when the selection carries stale groups", () => {
    renderWithProviders(
      <TradeTicket
        selection={{
          ...selection,
          freshnessStaleGroups: [
            {
              group_key: "mlb_weather",
              severity: "penalize",
              age_seconds: 7 * 3600,
              confidence_delta: -0.05,
              source: "load_weather",
            },
          ],
          freshnessConfidenceDelta: -0.05,
        }}
      />,
    );

    expect(screen.getByRole("group", { name: /stale feature/i })).toBeInTheDocument();
  });

  it("shows the Paper trade button but NOT the Demo order button", () => {
    // Demo order was removed from the ticket on 2026-05-18 — it's a
    // Kalshi-integration testing tool, not an operator workflow. The
    // /demo-orders endpoint and the dedicated /positions/demo page
    // still exist, but the trade ticket only surfaces paper.
    renderWithProviders(<TradeTicket selection={selection} />);
    expect(screen.getByRole("button", { name: /paper trade/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /demo order/i })).toBeNull();
  });
});
