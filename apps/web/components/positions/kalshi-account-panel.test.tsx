import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { KalshiAccountPanel } from "@/components/positions/kalshi-account-panel";
import type { PositionsRead } from "@/lib/types";
import { renderWithProviders } from "@/test/render";

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

const connectedPositions: PositionsRead = {
  paper_positions: [],
  demo_orders: [],
  kalshi_account: {
    configured: true,
    status: "connected",
    error_message: null,
    balance: {
      cash_balance_dollars: 125.5,
      portfolio_value_dollars: 171.25,
      updated_ts: 1711814400,
    },
    realized_pnl_dollars_total: 9.75,
    positions_truncated: false,
    realized_pnl_truncated: false,
    market_positions: [
      {
        ticker: "NBA-TEST",
        bet_label: "YES Celtics",
        bet_subtitle: "Celtics to win?",
        market_title: "Celtics to win?",
        market_subtitle: "NBA regular season",
        sport_key: "NBA",
        position: 3,
        total_traded_dollars: 1.65,
        market_exposure_dollars: 1.35,
        realized_pnl_dollars: 0.24,
        fees_paid_dollars: 0.01,
        resting_orders_count: 1,
        last_updated_ts: "2026-04-29T12:00:00Z",
      },
    ],
    recent_fills: [
      {
        fill_id: "fill-1",
        trade_id: "trade-1",
        order_id: "order-1",
        ticker: "NBA-FILL",
        bet_label: "YES Lakers",
        bet_subtitle: "Lakers to win?",
        market_title: "Lakers to win?",
        market_subtitle: "NBA late slate",
        sport_key: "NBA",
        side: "yes",
        action: "buy",
        count: 3,
        yes_price_dollars: 0.55,
        no_price_dollars: null,
        fee_dollars: 0.01,
        created_time: "2026-04-29T12:01:00Z",
      },
    ],
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
  // Bug #28 truncation flags + Smarter #32 drawdown brake snapshot.
  // Previously the hand-written interface marked these optional so
  // fixtures could skip them; the generated schema treats them as
  // always-present-on-the-wire (Pydantic defaults + nullable for the
  // brake).
  paper_truncated: false,
  demo_truncated: false,
  // PAPER_PARLAY_SCOPE.md step 3 added these to PositionsRead. The
  // Wire<> type marks them required even though the backend defaults
  // them to empty/false at serialization time.
  paper_parlays: [],
  paper_parlays_truncated: false,
  // Multi-user batch PR 3 added the legacy buckets.
  legacy_paper_positions: [],
  legacy_demo_orders: [],
  legacy_paper_parlays: [],
  legacy_paper_truncated: false,
  legacy_demo_truncated: false,
  legacy_paper_parlays_truncated: false,
  drawdown_brake: null,
};

describe("KalshiAccountPanel", () => {
  beforeEach(() => {
    mockFetchPositions.mockReset();
  });

  it("renders connected account picks, balances, and recent fills", async () => {
    mockFetchPositions.mockResolvedValue(connectedPositions);

    renderWithProviders(<KalshiAccountPanel />);

    expect(await screen.findByTestId("kalshi-account-panel")).toBeInTheDocument();
    expect(screen.getByText("$125.50")).toBeInTheDocument();
    expect(screen.getByText("$171.25")).toBeInTheDocument();
    expect(screen.getByTestId("kalshi-open-picks")).toHaveTextContent("1");
    expect(screen.getByRole("button", { name: /open picks 1/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByRole("button", { name: /recent fills 1/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByRole("columnheader", { name: "Bet" })).toBeInTheDocument();
    expect(screen.getByText("YES Celtics")).toBeInTheDocument();
    expect(screen.getByText("Celtics to win?")).toBeInTheDocument();
    expect(screen.getByText("NBA-TEST")).toBeInTheDocument();
    expect(screen.getByText("+$9.75")).toBeInTheDocument();
    expect(screen.getAllByText("+$0.24").length).toBeGreaterThan(0);
    expect(screen.queryByText("YES Lakers")).not.toBeInTheDocument();
    expect(screen.queryByText("$0.55")).not.toBeInTheDocument();
  });

  it("shows the NO price for NO-side fills, not the YES complement", async () => {
    const noFillPositions: PositionsRead = {
      ...connectedPositions,
      kalshi_account: {
        ...connectedPositions.kalshi_account,
        recent_fills: [
          {
            fill_id: "fill-no",
            trade_id: "trade-no",
            order_id: "order-no",
            ticker: "NBA-NO",
            bet_label: "NO Heat",
            bet_subtitle: "Heat to win?",
            market_title: "Heat to win?",
            market_subtitle: "NBA",
            sport_key: "NBA",
            side: "no",
            action: "buy",
            count: 2,
            yes_price_dollars: 0.9,
            no_price_dollars: 0.1,
            fee_dollars: 0.01,
            created_time: "2026-04-29T12:02:00Z",
          },
        ],
      },
    };
    const user = userEvent.setup();
    mockFetchPositions.mockResolvedValue(noFillPositions);

    renderWithProviders(<KalshiAccountPanel />);
    await user.click(await screen.findByRole("button", { name: /recent fills 1/i }));

    expect(screen.getByText("$0.10")).toBeInTheDocument(); // paid the NO price
    expect(screen.queryByText("$0.90")).not.toBeInTheDocument(); // not the YES complement
  });

  it("marks live totals as partial when the defensive page cap is hit", async () => {
    mockFetchPositions.mockResolvedValue({
      ...connectedPositions,
      kalshi_account: {
        ...connectedPositions.kalshi_account,
        positions_truncated: true,
        realized_pnl_truncated: true,
      },
    });

    renderWithProviders(<KalshiAccountPanel />);

    expect(await screen.findByTestId("kalshi-open-picks")).toHaveTextContent("≥1");
    expect(screen.getByText("+$9.75 · partial")).toBeInTheDocument();
    expect(screen.queryByText("≥+$9.75")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /open picks ≥1/i })).toBeInTheDocument();
  });

  it("toggles account tables independently", async () => {
    const user = userEvent.setup();
    mockFetchPositions.mockResolvedValue(connectedPositions);

    renderWithProviders(<KalshiAccountPanel />);

    const openPicksToggle = await screen.findByRole("button", { name: /open picks 1/i });
    const recentFillsToggle = screen.getByRole("button", { name: /recent fills 1/i });

    await user.click(recentFillsToggle);

    expect(recentFillsToggle).toHaveAttribute("aria-expanded", "true");
    expect(openPicksToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("YES Celtics")).toBeInTheDocument();
    expect(screen.getByText("YES Lakers")).toBeInTheDocument();
    expect(screen.getByText("$0.55")).toBeInTheDocument();

    await user.click(openPicksToggle);

    expect(openPicksToggle).toHaveAttribute("aria-expanded", "false");
    expect(recentFillsToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByText("YES Celtics")).not.toBeInTheDocument();
    expect(screen.getByText("YES Lakers")).toBeInTheDocument();
  });

  it("disables the Refresh button while the force-refresh is in flight", async () => {
    // Bug #6, codex round-12 P2: ``mutate(key, promise, { revalidate: false })``
    // doesn't set SWR's ``isValidating``, so without a local
    // ``isForcing`` flag a rapid double-click would issue multiple
    // ``/positions?force=true`` requests, each expiring the backend
    // cache and re-fetching from Kalshi. The button must disable
    // for the duration of the in-flight force request.
    let resolveForce!: (value: PositionsRead) => void;
    const forcePromise = new Promise<PositionsRead>((resolve) => {
      resolveForce = resolve;
    });
    mockFetchPositions.mockImplementation((options?: { force?: boolean }) => {
      if (options?.force) return forcePromise;
      return Promise.resolve(connectedPositions);
    });

    renderWithProviders(<KalshiAccountPanel />);
    await screen.findByTestId("kalshi-account-panel");

    const user = userEvent.setup();
    const [refreshButton] = await screen.findAllByRole("button", { name: /refresh/i });
    await user.click(refreshButton);

    // While the force fetch is still pending, the button must be disabled.
    expect(refreshButton).toBeDisabled();

    resolveForce(connectedPositions);
    // After the promise resolves, the button re-enables.
    await screen.findByRole("button", { name: /refresh/i }).then((button) => {
      expect(button).not.toBeDisabled();
    });
  });

  it("force-bypasses the cache when the Refresh button is clicked", async () => {
    // Bug #6, codex round-5 P2: backend caches /positions for ~30 s.
    // The Refresh button must pass force=true so users get fresh
    // Kalshi data without waiting out the TTL.
    mockFetchPositions.mockResolvedValue(connectedPositions);

    renderWithProviders(<KalshiAccountPanel />);
    await screen.findByTestId("kalshi-account-panel");
    // First call is the auto-load on mount; tests are about what the
    // Refresh button does.
    mockFetchPositions.mockClear();

    const user = userEvent.setup();
    const [refreshButton] = await screen.findAllByRole("button", { name: /refresh/i });
    await user.click(refreshButton);

    expect(mockFetchPositions).toHaveBeenCalled();
    expect(mockFetchPositions).toHaveBeenCalledWith({ force: true });
  });

  it("renders a setup state when account credentials are missing", async () => {
    mockFetchPositions.mockResolvedValue({
      paper_positions: [],
      demo_orders: [],
      kalshi_account: {
        configured: false,
        status: "not_configured",
        error_message: "Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH to connect your Kalshi account.",
        balance: null,
        market_positions: [],
        realized_pnl_dollars_total: null,
        positions_truncated: false,
        realized_pnl_truncated: false,
        recent_fills: [],
      },
    });

    renderWithProviders(<KalshiAccountPanel />);

    expect(await screen.findByText("Not configured")).toBeInTheDocument();
    expect(screen.getByText(/KALSHI_KEY_ID/)).toBeInTheDocument();
  });
});
