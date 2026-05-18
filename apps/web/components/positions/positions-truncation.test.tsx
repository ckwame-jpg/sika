// Bug #28 follow-up: integration tests pinning that the truncation
// hint actually appears (and only appears) when the API response
// reports the cap was hit. Complements ``truncation-hint.test.tsx``
// (which pins the component in isolation) by exercising the wire-up
// in both consuming tables.

import { render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DemoOrdersTable } from "@/components/positions/demo-orders-table";
import { PaperPositionsTable } from "@/components/positions/paper-positions-table";
import { PriceDisplayProvider } from "@/lib/price-display";
import type { PositionsRead } from "@/lib/types";

// Local provider wrapper — both consuming tables call
// ``usePriceDisplay()`` so they need the provider in scope. The
// shared ``renderWithProviders`` helper doesn't supply it (it's
// scoped to SWR only), so wrap inline here rather than widening the
// shared helper for one test file.
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
      recent_fills: [],
    },
    paper_truncated: false,
    demo_truncated: false,
    // PAPER_PARLAY_SCOPE.md step 3 added these fields to PositionsRead.
    // They're required by the generated Wire<> type even though the
    // backend defaults them to empty/false; fixtures must supply them.
    paper_parlays: [],
    paper_parlays_truncated: false,
    // Multi-user batch PR 3 added legacy buckets. Same fixture-must-
    // supply pattern as the paper_parlays addition above.
    legacy_paper_positions: [],
    legacy_demo_orders: [],
    legacy_paper_parlays: [],
    drawdown_brake: null,
    ...overrides,
  };
}

describe("PaperPositionsTable truncation hint", () => {
  beforeEach(() => {
    mockFetchPositions.mockReset();
  });

  it("renders the hint when paper_truncated is true", async () => {
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

    renderWithProviders(<PaperPositionsTable />);

    await waitFor(() => {
      expect(screen.getByText(/3 most recent/)).toBeInTheDocument();
    });
    expect(screen.getByText("paper_limit")).toBeInTheDocument();
  });

  it("does NOT render the hint when paper_truncated is false", async () => {
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
      }),
    );

    renderWithProviders(<PaperPositionsTable />);

    // Wait for SWR to settle with the mocked response.
    await waitFor(() => {
      expect(mockFetchPositions).toHaveBeenCalled();
    });
    expect(screen.queryByText(/most recent/)).not.toBeInTheDocument();
  });

  it("does NOT render the hint in the compact (sidebar) variant even when truncated", async () => {
    // Compact variant fits a fixed maxHeight; the banner would
    // crowd out the rows it's meant to introduce. Hide it there.
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

    renderWithProviders(<PaperPositionsTable maxHeight="200px" />);

    await waitFor(() => {
      expect(mockFetchPositions).toHaveBeenCalled();
    });
    expect(screen.queryByText(/most recent/)).not.toBeInTheDocument();
  });
});

describe("DemoOrdersTable truncation hint", () => {
  beforeEach(() => {
    mockFetchPositions.mockReset();
  });

  it("renders the hint when demo_truncated is true", async () => {
    mockFetchPositions.mockResolvedValue(
      buildResponse({
        demo_orders: Array.from({ length: 4 }, (_, i) => ({
          id: i + 1,
          ticker: `T-${i}`,
          client_order_id: `c-${i}`,
          kalshi_order_id: null,
          side: "yes",
          action: "buy",
          quantity: 1,
          limit_price: 0.5,
          status: "resting",
          approved_by_user: true,
          submitted_at: "2026-05-15T19:00:00Z",
          last_synced_at: "2026-05-15T19:00:00Z",
        })),
        demo_truncated: true,
      }),
    );

    renderWithProviders(<DemoOrdersTable />);

    await waitFor(() => {
      expect(screen.getByText(/4 most recent/)).toBeInTheDocument();
    });
    expect(screen.getByText("demo_limit")).toBeInTheDocument();
  });

  it("does NOT render the hint when demo_truncated is false", async () => {
    mockFetchPositions.mockResolvedValue(
      buildResponse({
        demo_orders: [
          {
            id: 1,
            ticker: "T-1",
            client_order_id: "c-1",
            kalshi_order_id: null,
            side: "yes",
            action: "buy",
            quantity: 1,
            limit_price: 0.5,
            status: "resting",
            approved_by_user: true,
            submitted_at: "2026-05-15T19:00:00Z",
            last_synced_at: "2026-05-15T19:00:00Z",
          },
        ],
        demo_truncated: false,
      }),
    );

    renderWithProviders(<DemoOrdersTable />);

    await waitFor(() => {
      expect(mockFetchPositions).toHaveBeenCalled();
    });
    expect(screen.queryByText(/most recent/)).not.toBeInTheDocument();
  });
});
