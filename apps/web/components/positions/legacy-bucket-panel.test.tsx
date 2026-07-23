import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import { renderWithProviders } from "@/test/render";
import { LegacyBucketPanel } from "./legacy-bucket-panel";
import type {
  DemoOrderRead,
  PaperParlayRead,
  PaperPositionRead,
  PositionsRead,
} from "@/lib/types";

const { mockFetchPositions } = vi.hoisted(() => ({
  mockFetchPositions: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchPositions: mockFetchPositions };
});

function basePositions(): PositionsRead {
  return {
    paper_positions: [],
    demo_orders: [],
    kalshi_account: {
      configured: false, status: "not_configured", error_message: null,
      balance: null, market_positions: [], realized_pnl_dollars_total: null,
      positions_truncated: false, realized_pnl_truncated: false, recent_fills: [],
    },
    paper_totals: {
      open_count: 0, closed_count: 0, open_exposure_dollars: 0,
      realized_pnl_dollars: 0, pending_parlay_count: 0,
      settled_parlay_count: 0, pending_parlay_exposure_dollars: 0,
      parlay_realized_pnl_dollars: 0, settled_7d_count: 0,
      wins_7d_count: 0, realized_pnl_7d_dollars: 0,
    },
    paper_truncated: false,
    demo_truncated: false,
    paper_parlays: [],
    paper_parlays_truncated: false,
    legacy_paper_positions: [],
    legacy_demo_orders: [],
    legacy_paper_parlays: [],
    legacy_paper_truncated: false,
    legacy_demo_truncated: false,
    legacy_paper_parlays_truncated: false,
    drawdown_brake: null,
  };
}

function legacyPosition(overrides: Partial<PaperPositionRead> = {}): PaperPositionRead {
  return {
    id: 100, ticker: "LEG-TICK", side: "yes", quantity: 1,
    entry_price: 0.42, exit_price: null, status: "open", pnl: null,
    notes: null, opened_at: "2025-12-01T10:00:00Z", closed_at: null,
    ...overrides,
  };
}

function legacyOrder(overrides: Partial<DemoOrderRead> = {}): DemoOrderRead {
  return {
    id: 200, ticker: "LEG-DEMO", client_order_id: "abc",
    kalshi_order_id: null, side: "yes", action: "buy", quantity: 1,
    limit_price: 0.5, status: "submitted", approved_by_user: true,
    submitted_at: "2025-12-01T10:00:00Z", last_synced_at: null,
    ...overrides,
  };
}

function legacyParlay(overrides: Partial<PaperParlayRead> = {}): PaperParlayRead {
  return {
    id: 300, created_at: "2025-12-01T10:00:00Z", stake: 50, leg_count: 2,
    sport_scope: "NBA", participating_sports: ["NBA"],
    combined_market_price: 0.25, combined_model_probability: 0.4,
    american_odds: "+300", edge: 0.15, notes: null,
    settlement_status: "pending", outcome: "pending",
    realized_pnl: null, settled_at: null, settlement_notes: null,
    legs: [],
    ...overrides,
  };
}

beforeEach(() => {
  mockFetchPositions.mockReset();
});

describe("LegacyBucketPanel", () => {
  it("renders nothing when every legacy list is empty", async () => {
    mockFetchPositions.mockResolvedValue(basePositions());
    const { container } = renderWithProviders(<LegacyBucketPanel />);
    await waitFor(() => expect(mockFetchPositions).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("renders the panel when at least one legacy list has rows", async () => {
    mockFetchPositions.mockResolvedValue({
      ...basePositions(),
      legacy_paper_positions: [legacyPosition()],
    });
    renderWithProviders(<LegacyBucketPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("legacy-bucket-panel")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("legacy-paper-position-100")).toBeInTheDocument();
  });

  it("counts total legacy rows in the panel title", async () => {
    mockFetchPositions.mockResolvedValue({
      ...basePositions(),
      legacy_paper_positions: [legacyPosition({ id: 1 }), legacyPosition({ id: 2 })],
      legacy_paper_parlays: [legacyParlay({ id: 10 })],
    });
    renderWithProviders(<LegacyBucketPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("legacy-bucket-panel")).toBeInTheDocument(),
    );
    // 2 positions + 1 parlay = 3 total.
    const heading = screen.getByText(/Legacy/i).closest("h2");
    expect(heading?.textContent ?? "").toMatch(/3/);
  });

  it("toggles collapsed/expanded state on header click", async () => {
    mockFetchPositions.mockResolvedValue({
      ...basePositions(),
      legacy_paper_parlays: [legacyParlay()],
    });
    renderWithProviders(<LegacyBucketPanel />);
    await waitFor(() => expect(screen.getByTestId("legacy-bucket-panel")).toBeInTheDocument());
    // Starts expanded — parlay row visible.
    expect(screen.getByTestId("legacy-paper-parlay-300")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByTestId("legacy-bucket-toggle"));
    expect(screen.queryByTestId("legacy-paper-parlay-300")).toBeNull();
    // Toggle back — visible again.
    await user.click(screen.getByTestId("legacy-bucket-toggle"));
    expect(screen.getByTestId("legacy-paper-parlay-300")).toBeInTheDocument();
  });

  it("renders all three legacy sub-tables when each has rows", async () => {
    mockFetchPositions.mockResolvedValue({
      ...basePositions(),
      legacy_paper_positions: [legacyPosition({ id: 1 })],
      legacy_demo_orders: [legacyOrder({ id: 2 })],
      legacy_paper_parlays: [legacyParlay({ id: 3 })],
    });
    renderWithProviders(<LegacyBucketPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("legacy-bucket-panel")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("legacy-paper-positions")).toBeInTheDocument();
    expect(screen.getByTestId("legacy-demo-orders")).toBeInTheDocument();
    expect(screen.getByTestId("legacy-paper-parlays")).toBeInTheDocument();
  });

  it("formats settled pnl with sign-before-dollar (decision applied to legacy too)", async () => {
    mockFetchPositions.mockResolvedValue({
      ...basePositions(),
      legacy_paper_parlays: [
        legacyParlay({ id: 1, outcome: "won", realized_pnl: 150, settlement_status: "settled" }),
        legacyParlay({ id: 2, outcome: "lost", realized_pnl: -50, settlement_status: "settled" }),
      ],
    });
    renderWithProviders(<LegacyBucketPanel />);
    await waitFor(() => expect(screen.getByTestId("legacy-paper-parlay-1")).toBeInTheDocument());
    expect(screen.getByTestId("legacy-paper-parlay-1")).toHaveTextContent("+$150.00");
    expect(screen.getByTestId("legacy-paper-parlay-2")).toHaveTextContent("-$50.00");
  });

  it("surfaces truncation for capped legacy rows", async () => {
    mockFetchPositions.mockResolvedValue({
      ...basePositions(),
      legacy_paper_positions: [legacyPosition()],
      legacy_paper_truncated: true,
    });

    renderWithProviders(<LegacyBucketPanel />);

    expect(await screen.findByText(/1 most recent/)).toBeInTheDocument();
    expect(screen.getByText("paper_limit")).toBeInTheDocument();
  });
});
