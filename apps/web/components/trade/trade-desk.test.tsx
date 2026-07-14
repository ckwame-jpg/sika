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
  it("renders the glass-instrument gauge row from trade-desk data", async () => {
    mockFetchTradeDesk.mockResolvedValue(tradeDeskFixture);

    renderWithProviders(<TradeDesk sport="NBA" />);

    await screen.findByText("Miami Heat at Toronto Raptors");
    // Collapsed by default: the event renders as a strip, not a panel.
    const eventToggle = screen.getByRole("button", { name: /miami heat at toronto raptors/i });
    expect(eventToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("trade-pick-row")).not.toBeInTheDocument();
    expect(eventToggle).toHaveTextContent("7 picks");

    // Gauge row — slate health / avg edge / top quartile / events orb.
    expect(screen.getByTestId("trade-gauge-health")).toHaveTextContent("fresh");
    expect(screen.getByTestId("trade-gauge-health")).toHaveTextContent("7 of 7 scored");

    // Fixture edges: [0.08, 0.09, 0.10, 0.321, 0.098, 0.044, 0.051]
    // mean = 0.1120 → "+11.2%"; nearest-rank p75 = sorted[5] = 0.10 → "+10.0%"
    expect(screen.getByTestId("trade-gauge-avg-edge")).toHaveTextContent("+11.2%");
    expect(screen.getByTestId("trade-gauge-avg-edge")).toHaveTextContent("gauge vs +10% cap");
    expect(screen.getByTestId("trade-gauge-top-quartile")).toHaveTextContent("+10.0%");
    expect(screen.getByTestId("trade-gauge-top-quartile")).toHaveTextContent("7 picks past bar");
    expect(screen.getByTestId("trade-gauge-events")).toHaveTextContent("1 · 0 live");

    await waitFor(() => {
      expect(mockFetchTradeDesk).toHaveBeenCalledWith("NBA");
    });
    expect(mockFetchPositions).not.toHaveBeenCalled();
  });

  it("expands an event into edge-sorted pick rows and loads the ticket on row click", async () => {
    const user = userEvent.setup();
    mockFetchTradeDesk.mockResolvedValue(tradeDeskFixture);

    renderWithProviders(<TradeDesk sport="NBA" />);
    await screen.findByText("Miami Heat at Toronto Raptors");
    await user.click(screen.getByRole("button", { name: /miami heat at toronto raptors/i }));

    // Flattened picks (3 game lines + 4 prop thresholds), sorted by edge desc.
    const rows = await screen.findAllByTestId("trade-pick-row");
    expect(rows).toHaveLength(7);
    expect(rows[0]).toHaveTextContent("Davion Mitchell 10+ points");
    expect(rows[0]).toHaveClass("gi-hero-row");
    expect(rows[1]).toHaveTextContent("Over 219.5");
    // Full-game winner keeps its market-kind tag.
    const fgRow = rows.find((row) => row.textContent?.includes("Toronto Raptors to win"));
    expect(fgRow).toBeDefined();
    expect(within(fgRow!).getByTestId("line-row-kind-badge")).toHaveTextContent("FG");

    await user.click(rows[0]);
    expectAnyTicketTitleToContain("Davion Mitchell 10+ points");
    expect(rows[0]).toHaveClass("selected");

    // Selecting a different row swaps the ticket.
    const assistsRow = rows.find((row) => row.textContent?.includes("Davion Mitchell 4+ assists"));
    await user.click(assistsRow!);
    expectAnyTicketTitleToContain("Davion Mitchell 4+ assists");
  });

  it("toggles event cards while preserving the selected trade ticket", async () => {
    const user = userEvent.setup();
    mockFetchTradeDesk.mockResolvedValue(tradeDeskFixture);

    renderWithProviders(<TradeDesk sport="NBA" />);

    const eventToggle = await screen.findByRole("button", { name: /miami heat at toronto raptors/i });
    expect(screen.getByTestId("trade-ticket-rail")).toHaveClass("trade-ticket-rail");

    await user.click(eventToggle);
    // Expanded: the toggle re-renders as the panel head.
    const panelToggle = screen.getByRole("button", { name: /miami heat at toronto raptors/i });
    expect(panelToggle).toHaveAttribute("aria-expanded", "true");

    const rows = await screen.findAllByTestId("trade-pick-row");
    const assistsRow = rows.find((row) => row.textContent?.includes("Davion Mitchell 4+ assists"));
    await user.click(assistsRow!);
    expectAnyTicketTitleToContain("Davion Mitchell 4+ assists");

    await user.click(panelToggle);
    const stripToggle = screen.getByRole("button", { name: /miami heat at toronto raptors/i });
    expect(stripToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("trade-pick-row")).not.toBeInTheDocument();
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
      blocking_reason: "Current NBA/MLB/WNBA events exist, but no current Kalshi markets are mapped to them.",
    });

    renderWithProviders(<TradeDesk sport="NBA" />);

    await screen.findByText("Current slate is degraded for NBA.");
    expect(screen.getByTestId("trade-desk-status-pill")).toHaveTextContent("Current NBA/MLB/WNBA events exist");
    expect(screen.getByTestId("trade-gauge-health")).toHaveTextContent("degraded");
    expect(screen.getByText("Current events")).toBeInTheDocument();
    expect(screen.getAllByText("Candidate markets").length).toBeGreaterThanOrEqual(1);
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
      blocking_reason: "Current NBA/MLB/WNBA events exist, but no current Kalshi markets are mapped to them.",
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
    expect(screen.getByTestId("trade-gauge-top-quartile")).toHaveTextContent("0 picks past bar");

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

  it("keeps coverage-only events visible without treating them as bets", async () => {
    const user = userEvent.setup();
    mockFetchTradeDesk.mockResolvedValue({
      ...tradeDeskFixture,
      events: [
        {
          event_id: 22,
          event_name: "Boston Celtics at Philadelphia 76ers",
          event_status: "scheduled",
          starts_at: "2026-04-08T00:00:00Z",
          sport_key: "NBA",
          candidate_market_count: 4,
          scored_market_count: 4,
          coverage_prediction_count: 4,
          game_lines: [],
          player_props: [],
        },
      ],
      event_count: 1,
      candidate_market_count: 4,
      scored_market_count: 4,
      recommendation_count: 0,
      coverage_prediction_count: 4,
    });

    renderWithProviders(<TradeDesk sport="NBA" />);

    const eventToggle = await screen.findByRole("button", { name: /boston celtics at philadelphia 76ers/i });
    expect(eventToggle).toHaveTextContent("0 picks");
    expect(eventToggle).toHaveTextContent("4 coverage");

    await user.click(eventToggle);

    expect(screen.getByText("Coverage")).toBeInTheDocument();
    expect(screen.getByText(/No bet cleared bet filters/)).toBeInTheDocument();
    expect(screen.queryByTestId("trade-pick-row")).not.toBeInTheDocument();
  });

  it("shows a retry button on fetch error that refetches and renders the desk", async () => {
    // The trade-desk endpoint is the heaviest call in the app and
    // bears the brunt of API timeouts. The retry button replaces
    // the "hard-reload the whole page" recovery flow.
    mockFetchTradeDesk
      .mockRejectedValueOnce(new Error("Request timed out after 15s."))
      .mockResolvedValueOnce(tradeDeskFixture);

    renderWithProviders(<TradeDesk sport="NBA" />);

    await waitFor(() =>
      expect(screen.getByTestId("trade-desk-error")).toBeInTheDocument(),
    );
    // Surfaced for the operator — not just a generic "failed" message.
    expect(screen.getByText(/Request timed out/)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByTestId("trade-desk-retry"));

    await waitFor(() =>
      expect(screen.queryByTestId("trade-desk-error")).not.toBeInTheDocument(),
    );
    // Second call resolves with the fixture → desk renders the event.
    await screen.findByText("Miami Heat at Toronto Raptors");
    expect(mockFetchTradeDesk).toHaveBeenCalledTimes(2);
  });
});
