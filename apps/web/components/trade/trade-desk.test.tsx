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
  it("renders market KPIs from trade-desk data and never depends on positions", async () => {
    mockFetchTradeDesk.mockResolvedValue(tradeDeskFixture);

    renderWithProviders(<TradeDesk sport="NBA" />);

    await screen.findByText("Miami Heat at Toronto Raptors");
    expect(screen.getByTestId("trade-kpi-events")).toHaveTextContent("1");
    expect(screen.getByTestId("trade-kpi-game-lines")).toHaveTextContent("3");
    expect(screen.getByTestId("trade-kpi-prop-ladders")).toHaveTextContent("2");
    expect(screen.getByTestId("trade-kpi-thresholds")).toHaveTextContent("4");
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

    const propCard = screen.getByTestId("trade-prop-card");

    await user.click(within(propCard).getByRole("button", { name: "4+" }));
    expect(within(propCard).getByTestId("trade-prop-summary-label")).toHaveTextContent("4+ assists");
    expect(within(propCard).getByTestId("trade-prop-summary-win-prob")).toHaveTextContent("89.4%");
    expect(within(propCard).getByTestId("trade-prop-summary-edge")).toHaveTextContent("+4.4%");
    expectAnyTicketTitleToContain("Davion Mitchell 4+ assists");
    expect(within(propCard).getAllByTestId("trade-threshold-chip").filter((chip) => chip.getAttribute("aria-pressed") === "true")).toHaveLength(1);

    await user.click(within(propCard).getByRole("button", { name: "10+" }));
    expect(within(propCard).getByTestId("trade-prop-summary-label")).toHaveTextContent("10+ points");
    expect(within(propCard).getByTestId("trade-prop-summary-win-prob")).toHaveTextContent("72.1%");
    expect(within(propCard).getByTestId("trade-prop-summary-edge")).toHaveTextContent("+32.1%");
    expectAnyTicketTitleToContain("Davion Mitchell 10+ points");
    expect(within(propCard).getAllByTestId("trade-threshold-chip").filter((chip) => chip.getAttribute("aria-pressed") === "true")).toHaveLength(1);
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
    expect(screen.getByText("Candidate markets")).toBeInTheDocument();
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
