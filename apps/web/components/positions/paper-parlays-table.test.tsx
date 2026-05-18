import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import { renderWithProviders } from "@/test/render";
import { PaperParlaysTable } from "./paper-parlays-table";
import type { PaperParlayRead, PositionsRead } from "@/lib/types";

const { mockFetchPositions } = vi.hoisted(() => ({
  mockFetchPositions: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchPositions: mockFetchPositions };
});

function emptyPositions(): PositionsRead {
  return {
    paper_positions: [],
    demo_orders: [],
    kalshi_account: {
      configured: false,
      status: "not_configured",
      error_message: null,
      balance: null,
      market_positions: [],
      recent_fills: [],
    },
    paper_truncated: false,
    demo_truncated: false,
    paper_parlays: [],
    paper_parlays_truncated: false,
    legacy_paper_positions: [],
    legacy_demo_orders: [],
    legacy_paper_parlays: [],
    drawdown_brake: null,
  };
}

function parlayRow(overrides: Partial<PaperParlayRead> = {}): PaperParlayRead {
  return {
    id: 1,
    created_at: "2026-05-17T19:00:00Z",
    stake: 100,
    leg_count: 2,
    sport_scope: "NBA",
    participating_sports: ["NBA"],
    combined_market_price: 0.25,
    combined_model_probability: 0.44,
    american_odds: "+300",
    edge: 0.19,
    notes: null,
    settlement_status: "pending",
    outcome: "pending",
    realized_pnl: null,
    settled_at: null,
    settlement_notes: null,
    legs: [
      {
        id: 1,
        leg_index: 0,
        source_prediction_id: 10,
        market_id: 100,
        ticker: "A-TICKER",
        sport_key: "NBA",
        event_name: "Cleveland Cavaliers at Detroit Pistons",
        market_title: "Donovan Mitchell points",
        market_kind: "player_prop",
        stat_key: "points",
        threshold: 25,
        subject_name: "Donovan Mitchell",
        subject_team: "CLE",
        side: "yes",
        suggested_price: 0.55,
        fair_yes_price: 0.6,
        fair_no_price: 0.4,
      },
      {
        id: 2,
        leg_index: 1,
        source_prediction_id: 11,
        market_id: 101,
        ticker: "B-TICKER",
        sport_key: "NBA",
        event_name: "Cleveland Cavaliers at Detroit Pistons",
        market_title: "Jalen Duren rebounds",
        market_kind: "player_prop",
        stat_key: "rebounds",
        threshold: 10,
        subject_name: "Jalen Duren",
        subject_team: "DET",
        side: "yes",
        suggested_price: 0.45,
        fair_yes_price: 0.5,
        fair_no_price: 0.5,
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  mockFetchPositions.mockReset();
});

describe("PaperParlaysTable", () => {
  it("renders an empty state when no parlays exist", async () => {
    mockFetchPositions.mockResolvedValue(emptyPositions());
    renderWithProviders(<PaperParlaysTable />);
    await waitFor(() =>
      expect(screen.getByTestId("paper-parlays-empty")).toBeInTheDocument(),
    );
  });

  it("renders a row per parlay with status pill + stake", async () => {
    mockFetchPositions.mockResolvedValue({
      ...emptyPositions(),
      paper_parlays: [parlayRow({ id: 1 })],
    });
    renderWithProviders(<PaperParlaysTable />);
    await waitFor(() =>
      expect(screen.getByTestId("paper-parlay-row-1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("paper-parlay-status-1")).toHaveTextContent("pending");
    expect(screen.getByTestId("paper-parlay-row-1")).toHaveTextContent("$100.00");
  });

  it("expanding a row reveals the per-leg detail panel", async () => {
    mockFetchPositions.mockResolvedValue({
      ...emptyPositions(),
      paper_parlays: [parlayRow({ id: 7, notes: "test parlay" })],
    });
    renderWithProviders(<PaperParlaysTable />);
    await waitFor(() => expect(screen.getByTestId("paper-parlay-row-7")).toBeInTheDocument());

    expect(screen.queryByTestId("paper-parlay-detail-7")).toBeNull();
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("Expand legs"));
    const detail = screen.getByTestId("paper-parlay-detail-7");
    expect(detail).toBeInTheDocument();
    expect(detail).toHaveTextContent("test parlay");
    // Both legs' summary lines appear.
    expect(detail).toHaveTextContent(/donovan mitchell/i);
    expect(detail).toHaveTextContent(/jalen duren/i);
  });

  it("colors won/lost outcomes via the status pill class", async () => {
    mockFetchPositions.mockResolvedValue({
      ...emptyPositions(),
      paper_parlays: [
        parlayRow({ id: 1, outcome: "won", realized_pnl: 300, settlement_status: "settled" }),
        parlayRow({ id: 2, outcome: "lost", realized_pnl: -100, settlement_status: "settled" }),
      ],
    });
    renderWithProviders(<PaperParlaysTable />);
    await waitFor(() =>
      expect(screen.getByTestId("paper-parlay-row-1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("paper-parlay-status-1")).toHaveClass("won");
    expect(screen.getByTestId("paper-parlay-status-2")).toHaveClass("lost");
    // PnL formatting: positive prefixed with "+", negative natural sign.
    expect(screen.getByTestId("paper-parlay-row-1")).toHaveTextContent("+$300.00");
    expect(screen.getByTestId("paper-parlay-row-2")).toHaveTextContent("-$100.00");
  });

  it("shows the truncation hint when the API capped the response", async () => {
    mockFetchPositions.mockResolvedValue({
      ...emptyPositions(),
      paper_parlays: [parlayRow({ id: 1 })],
      paper_parlays_truncated: true,
    });
    renderWithProviders(<PaperParlaysTable />);
    await waitFor(() => {
      expect(screen.getByText(/paper_limit/)).toBeInTheDocument();
    });
  });
});
