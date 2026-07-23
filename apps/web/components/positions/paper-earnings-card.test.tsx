import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ExposureRail,
  PaperGaugeRow,
} from "@/components/positions/paper-earnings-card";
import type { PositionsRead } from "@/lib/types";
import { renderWithProviders } from "@/test/render";

const { mockFetchPositions } = vi.hoisted(() => ({
  mockFetchPositions: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchPositions: mockFetchPositions };
});

function positionsResponse(): PositionsRead {
  return {
    paper_positions: [],
    demo_orders: [],
    kalshi_account: {
      configured: false,
      status: "not_configured",
      error_message: null,
      balance: null,
      market_positions: [],
      realized_pnl_dollars_total: null,
      positions_truncated: false,
      realized_pnl_truncated: false,
      recent_fills: [],
    },
    paper_totals: {
      open_count: 1,
      closed_count: 201,
      open_exposure_dollars: 400,
      realized_pnl_dollars: 80,
      pending_parlay_count: 1,
      settled_parlay_count: 10,
      pending_parlay_exposure_dollars: 100,
      parlay_realized_pnl_dollars: 20,
      settled_7d_count: 4,
      wins_7d_count: 3,
      realized_pnl_7d_dollars: 75,
    },
    paper_truncated: true,
    demo_truncated: false,
    paper_parlays: [],
    paper_parlays_truncated: true,
    legacy_paper_positions: [],
    legacy_demo_orders: [],
    legacy_paper_parlays: [],
    legacy_paper_truncated: false,
    legacy_demo_truncated: false,
    legacy_paper_parlays_truncated: false,
    drawdown_brake: null,
  };
}

describe("paper portfolio totals", () => {
  beforeEach(() => {
    mockFetchPositions.mockReset();
    window.localStorage.clear();
    mockFetchPositions.mockResolvedValue(positionsResponse());
  });

  it("renders KPI gauges from exact server totals, not capped arrays", async () => {
    renderWithProviders(<PaperGaugeRow />);

    expect(await screen.findByTestId("paper-earnings-grid")).toBeInTheDocument();
    expect(screen.getByTestId("paper-earnings-bankroll")).toHaveTextContent(
      "$1,100.00",
    );
    expect(screen.getByTestId("paper-earnings-open")).toHaveTextContent("$500.00");
    expect(screen.getByTestId("paper-earnings-realized")).toHaveTextContent(
      "+$75.00",
    );
    expect(screen.getByText("75% win · 4 settled")).toBeInTheDocument();
    expect(screen.getByText("2 open")).toBeInTheDocument();
    expect(screen.getByText("1 single · 1 parlay")).toBeInTheDocument();
  });

  it("uses exact exposure buckets and links to the full server CSV", async () => {
    renderWithProviders(<ExposureRail />);

    expect(await screen.findByText("$400.00 · 80%")).toBeInTheDocument();
    expect(screen.getByText("$100.00 · 20%")).toBeInTheDocument();
    expect(screen.getByTestId("portfolio-export-ledger")).toHaveAttribute(
      "href",
      "/api/positions/export",
    );
  });

  it("does not claim today's activity is empty when source lists are truncated", async () => {
    renderWithProviders(<ExposureRail />);

    expect(
      await screen.findByText(
        "No settlements in the loaded sample; older bets are not shown.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("nothing settled yet today.")).not.toBeInTheDocument();
  });

  it("uses the exact empty sentinel when every activity list is complete", async () => {
    mockFetchPositions.mockResolvedValue({
      ...positionsResponse(),
      paper_truncated: false,
      paper_parlays_truncated: false,
      legacy_paper_truncated: false,
      legacy_paper_parlays_truncated: false,
    });

    renderWithProviders(<ExposureRail />);

    expect(await screen.findByText("nothing settled yet today.")).toBeInTheDocument();
  });
});
