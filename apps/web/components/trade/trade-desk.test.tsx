import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TradeDesk } from "@/components/trade/trade-desk";
import { tradeDeskFixture } from "@/test/fixtures/trade-fixtures";
import { renderWithProviders } from "@/test/render";

const { mockFetchTradeDesk, mockFetchPositions } = vi.hoisted(() => ({
  mockFetchTradeDesk: vi.fn(),
  mockFetchPositions: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchTradeDesk: mockFetchTradeDesk,
    fetchPositions: mockFetchPositions,
  };
});

vi.mock("@/components/positions/trade-dialog", () => ({
  TradeDialog: () => null,
}));

function expectAnyTicketTitleToContain(expected: string) {
  const ticketTitles = screen.getAllByTestId("trade-ticket-title");
  expect(ticketTitles.some((node) => node.textContent?.includes(expected))).toBe(true);
}

describe("TradeDesk", () => {
  it("renders the Phase 1 KPI quad and hero chips from trade-desk data", async () => {
    mockFetchTradeDesk.mockResolvedValue(tradeDeskFixture);

    renderWithProviders(<TradeDesk sport="NBA" />);

    await screen.findByText("Miami Heat at Toronto Raptors");
    const eventToggle = screen.getByRole("button", { name: /miami heat at toronto raptors/i });
    expect(eventToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Game Lines")).not.toBeInTheDocument();
    expect(screen.queryByTestId("trade-prop-card")).not.toBeInTheDocument();

    // KPI quad — 4 cards, new labels + sub-lines
    expect(screen.getByTestId("trade-kpi-events")).toHaveTextContent("1");
    expect(screen.getByTestId("trade-kpi-card-events")).toHaveTextContent("Events on the board");
    expect(screen.getByTestId("trade-kpi-card-events")).toHaveTextContent("0 live · 1 upcoming");

    expect(screen.getByTestId("trade-kpi-candidate-markets")).toHaveTextContent("7");
    expect(screen.getByTestId("trade-kpi-card-candidate-markets")).toHaveTextContent("Candidate markets");
    expect(screen.getByTestId("trade-kpi-card-candidate-markets")).toHaveTextContent("scored");

    expect(screen.getByTestId("trade-kpi-recommendations")).toHaveTextContent("7");
    expect(screen.getByTestId("trade-kpi-card-recommendations")).toHaveTextContent("Current picks");
    expect(screen.getByTestId("trade-kpi-card-recommendations")).toHaveTextContent("past edge threshold");

    // Fixture edges: [0.08, 0.09, 0.10, 0.321, 0.098, 0.044, 0.051]
    // mean = 0.1120 → "+11.2%"; nearest-rank p75 = sorted[5] = 0.10 → "+10.0%"
    expect(screen.getByTestId("trade-kpi-avg-edge")).toHaveTextContent("+11.2%");
    expect(screen.getByTestId("trade-kpi-card-avg-edge")).toHaveTextContent("Avg edge");
    expect(screen.getByTestId("trade-kpi-card-avg-edge")).toHaveTextContent("top-quartile +10.0%");

    // Hero: two-clause headline + chip row
    expect(screen.getByText(/markets in current snapshot\./)).toBeInTheDocument();
    expect(screen.getByText(/current picks\./)).toBeInTheDocument();
    expect(screen.getByTestId("trade-hero-chip-avg-edge")).toHaveTextContent("+11.2%");
    expect(screen.getByTestId("trade-hero-chip-top-quartile")).toHaveTextContent("+10.0%");

    // Filter tabs removed; positions never fetched
    expect(screen.queryByRole("button", { name: "Player Props" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Game Lines" })).not.toBeInTheDocument();
    expect(screen.queryByText("Your Exposure")).not.toBeInTheDocument();
    expect(screen.queryByText("Event Context")).not.toBeInTheDocument();

    await waitFor(() => {
      expect(mockFetchTradeDesk).toHaveBeenCalledWith("NBA");
    });
    expect(mockFetchPositions).not.toHaveBeenCalled();
  });

  it("keeps the prop-card header, selected chip, and ticket in sync across threshold clicks", async () => {
    const user = userEvent.setup();
    mockFetchTradeDesk.mockResolvedValue(tradeDeskFixture);

    renderWithProviders(<TradeDesk sport="NBA" />);
    await screen.findByText("Miami Heat at Toronto Raptors");
    await user.click(screen.getByRole("button", { name: /miami heat at toronto raptors/i }));

    const propCard = await screen.findByTestId("trade-prop-card");

    await user.click(within(propCard).getByRole("button", { name: "4+" }));
    expect(within(propCard).getByTestId("trade-prop-summary-label")).toHaveTextContent("4+ assists");
    expect(within(propCard).getByText("89.4%")).toBeInTheDocument();
    expect(within(propCard).getByTestId("trade-prop-summary-edge")).toHaveTextContent("+4.4%");
    expectAnyTicketTitleToContain("Davion Mitchell 4+ assists");
    expect(within(propCard).getAllByTestId("trade-threshold-chip").filter((chip) => chip.getAttribute("aria-pressed") === "true")).toHaveLength(1);

    await user.click(within(propCard).getByRole("button", { name: "10+" }));
    expect(within(propCard).getByTestId("trade-prop-summary-label")).toHaveTextContent("10+ points");
    expect(within(propCard).getByText("72.1%")).toBeInTheDocument();
    expect(within(propCard).getByTestId("trade-prop-summary-edge")).toHaveTextContent("+32.1%");
    expectAnyTicketTitleToContain("Davion Mitchell 10+ points");
    expect(within(propCard).getAllByTestId("trade-threshold-chip").filter((chip) => chip.getAttribute("aria-pressed") === "true")).toHaveLength(1);
  });

  it("toggles event cards while preserving the selected trade ticket", async () => {
    const user = userEvent.setup();
    mockFetchTradeDesk.mockResolvedValue(tradeDeskFixture);

    renderWithProviders(<TradeDesk sport="NBA" />);

    const eventToggle = await screen.findByRole("button", { name: /miami heat at toronto raptors/i });
    expect(screen.getByTestId("trade-ticket-rail")).toHaveClass("trade-ticket-rail");

    await user.click(eventToggle);
    expect(eventToggle).toHaveAttribute("aria-expanded", "true");

    const propCard = await screen.findByTestId("trade-prop-card");
    await user.click(within(propCard).getByRole("button", { name: "4+" }));
    expectAnyTicketTitleToContain("Davion Mitchell 4+ assists");

    await user.click(eventToggle);
    expect(eventToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("trade-prop-card")).not.toBeInTheDocument();
    expectAnyTicketTitleToContain("Davion Mitchell 4+ assists");
  });

  it("renders degraded slate health instead of a generic empty state", async () => {
    mockFetchTradeDesk.mockResolvedValue({
      ...tradeDeskFixture,
      events: [],
      freshness_status: "degraded",
      event_count: 2,
      candidate_market_count: 0,
      scored_market_count: 0,
      recommendation_count: 0,
      coverage_prediction_count: 0,
      blocking_reason: "Current NBA/MLB events exist, but no current Kalshi markets are mapped to them.",
    });

    renderWithProviders(<TradeDesk sport="NBA" />);

    await screen.findByText("Current slate is degraded for NBA.");
    expect(screen.getByTestId("trade-desk-status-pill")).toHaveTextContent("Current NBA/MLB events exist");
    expect(screen.getByText("Current events")).toBeInTheDocument();
    // "Candidate markets" now appears both in SlateHealthDetails and the KPI quad label.
    expect(screen.getAllByText("Candidate markets").length).toBeGreaterThanOrEqual(2);
  });

  it("renders last good slate as a separate collapsible archive", async () => {
    const user = userEvent.setup();
    mockFetchTradeDesk.mockResolvedValue({
      ...tradeDeskFixture,
      events: [],
      freshness_status: "degraded",
      event_count: 2,
      candidate_market_count: 0,
      scored_market_count: 0,
      recommendation_count: 0,
      coverage_prediction_count: 0,
      blocking_reason: "Current NBA/MLB events exist, but no current Kalshi markets are mapped to them.",
      previous_slate: {
        events: tradeDeskFixture.events,
        generated_at: "2026-04-07T18:00:00Z",
        freshness_status: "stale",
        event_count: 1,
        candidate_market_count: 7,
        scored_market_count: 7,
        recommendation_count: 7,
        coverage_prediction_count: 0,
        blocking_reason: null,
        generated_from_run_id: 99,
      },
    });

    renderWithProviders(<TradeDesk sport="NBA" />);

    await screen.findByText("Current slate is degraded for NBA.");
    expect(screen.getByTestId("trade-kpi-recommendations")).toHaveTextContent("0");

    const archiveToggle = screen.getByRole("button", { name: /last good slate/i });
    expect(archiveToggle).toHaveAttribute("aria-expanded", "true");
    expect(archiveToggle).toHaveTextContent("7 picks");
    expect(screen.getByRole("button", { name: /miami heat at toronto raptors/i })).toBeInTheDocument();

    await user.click(archiveToggle);

    expect(archiveToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /miami heat at toronto raptors/i })).not.toBeInTheDocument();
  });

  it("renders empty scored slate state when no markets clear thresholds", async () => {
    mockFetchTradeDesk.mockResolvedValue({
      ...tradeDeskFixture,
      events: [],
      freshness_status: "empty",
      event_count: 2,
      candidate_market_count: 18,
      scored_market_count: 18,
      recommendation_count: 0,
      coverage_prediction_count: 18,
      blocking_reason: "Current slate scored successfully, but no markets cleared recommendation thresholds.",
    });

    renderWithProviders(<TradeDesk />);

    await screen.findByText("No markets cleared thresholds.");
    expect(screen.getByTestId("trade-desk-status-pill")).toHaveTextContent("no markets cleared");
    expect(screen.getByText("Scored markets")).toBeInTheDocument();
    expect(screen.getAllByText("18").length).toBeGreaterThan(0);
  });
});
