// Bug #28 follow-up: integration tests pinning that the truncation
// hint actually appears (and only appears) when the API response
// reports the cap was hit. Complements ``truncation-hint.test.tsx``
// (which pins the component in isolation) by exercising the wire-up
// in the consuming table.
//
// Updated in Phase 2 of the paper-trade redesign: the old per-kind
// tables (PaperPositionsTable / DemoOrdersTable / PaperParlaysTable)
// were replaced by the unified PaperBetsTable. The test now covers
// the merged table — both ``paper_truncated`` (singles cap) and
// ``paper_parlays_truncated`` (parlays cap) trigger the hint via
// the same surface.

import { render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PaperBetsTable } from "@/components/positions/paper-bets-table";
import { PriceDisplayProvider } from "@/lib/price-display";
import type { PositionsRead } from "@/lib/types";

function renderWithProviders(ui: ReactElement) {
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <SWRConfig
        value={{
          provider: () => new Map(),
          dedupingInterval: 0,
          shouldRetryOnError: false,
          revalidateOnFocus: false,
          revalidateOnReconnect: false,
        }}
      >
        <PriceDisplayProvider initialMode="kalshi">{children}</PriceDisplayProvider>
      </SWRConfig>
    );
  }
  return rtlRender(ui, { wrapper: Wrapper });
}

const { mockFetchPositions } = vi.hoisted(() => ({
  mockFetchPositions: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchPositions: mockFetchPositions,
  };
});

function buildResponse(overrides: Partial<PositionsRead> = {}): PositionsRead {
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
      open_count: 0,
      closed_count: 0,
      open_exposure_dollars: 0,
      realized_pnl_dollars: 0,
      pending_parlay_count: 0,
      settled_parlay_count: 0,
      pending_parlay_exposure_dollars: 0,
      parlay_realized_pnl_dollars: 0,
      settled_7d_count: 0,
      wins_7d_count: 0,
      realized_pnl_7d_dollars: 0,
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
    ...overrides,
  };
}

describe("PaperBetsTable truncation hint", () => {
  beforeEach(() => {
    mockFetchPositions.mockReset();
  });

  it("renders the hint when paper_truncated is true (singles cap hit)", async () => {
    mockFetchPositions.mockResolvedValue(
      buildResponse({
        paper_positions: Array.from({ length: 3 }, (_, i) => ({
          id: i + 1,
          ticker: `T-${i}`,
          side: "yes",
          quantity: 1,
          entry_price: 0.5,
          exit_price: null,
          status: "open",
          pnl: null,
          notes: null,
          opened_at: "2026-05-15T19:00:00Z",
          closed_at: null,
        })),
        paper_truncated: true,
      }),
    );

    renderWithProviders(<PaperBetsTable />);

    await waitFor(() => {
      expect(screen.getByText(/3 most recent/)).toBeInTheDocument();
    });
    expect(screen.getByText("paper_limit")).toBeInTheDocument();
  });

  it("renders the hint when paper_parlays_truncated is true (parlays cap hit)", async () => {
    mockFetchPositions.mockResolvedValue(
      buildResponse({
        paper_parlays_truncated: true,
      }),
    );

    renderWithProviders(<PaperBetsTable />);

    await waitFor(() => {
      expect(mockFetchPositions).toHaveBeenCalled();
    });
    expect(screen.getByText("paper_limit")).toBeInTheDocument();
  });

  it("does NOT render the hint when neither cap was hit", async () => {
    mockFetchPositions.mockResolvedValue(
      buildResponse({
        paper_positions: [
          {
            id: 1,
            ticker: "T-1",
            side: "yes",
            quantity: 1,
            entry_price: 0.5,
            exit_price: null,
            status: "open",
            pnl: null,
            notes: null,
            opened_at: "2026-05-15T19:00:00Z",
            closed_at: null,
          },
        ],
        paper_truncated: false,
        paper_parlays_truncated: false,
      }),
    );

    renderWithProviders(<PaperBetsTable />);

    await waitFor(() => {
      expect(mockFetchPositions).toHaveBeenCalled();
    });
    expect(screen.queryByText(/most recent/)).not.toBeInTheDocument();
  });
});
