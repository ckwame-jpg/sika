import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AutoTradingPanel } from "@/components/ops/auto-trading-panel";
import { renderWithProviders } from "@/test/render";

const { mockFetchAutoTradingStatus, mockFetchAutoTradeRuns } = vi.hoisted(() => ({
  mockFetchAutoTradingStatus: vi.fn(),
  mockFetchAutoTradeRuns: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchAutoTradingStatus: mockFetchAutoTradingStatus,
    fetchAutoTradeRuns: mockFetchAutoTradeRuns,
  };
});

describe("AutoTradingPanel", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("prompts for the owner token before loading live controls", async () => {
    renderWithProviders(<AutoTradingPanel />);

    expect(await screen.findByText("Owner Access")).toBeInTheDocument();
    expect(mockFetchAutoTradingStatus).not.toHaveBeenCalled();
  });

  it("renders budget and latest run when the owner token is present", async () => {
    window.localStorage.setItem("sika_owner_admin_token", "secret");
    mockFetchAutoTradingStatus.mockResolvedValue({
      enabled_by_env: true,
      kill_switch_active: false,
      effective_enabled: true,
      daily_budget_cents: 1000,
      spent_today_cents: 500,
      remaining_budget_cents: 500,
      max_orders_per_day: 5,
      local_trade_date: "2026-04-20",
      local_run_time: "10:15",
      market_scope: "nba_mlb_current_slate",
      allow_parlays: false,
      live_credentials_configured: true,
      latest_account_snapshot: null,
      latest_run: {
        id: 7,
        strategy_key: "nba_mlb_current_slate_v1",
        local_trade_date: "2026-04-20",
        requested_by: "manual",
        status: "completed",
        budget_cents: 1000,
        spent_cents: 500,
        candidate_count: 3,
        submitted_order_count: 1,
        skipped_reason: null,
        error_message: null,
        details: {},
        started_at: "2026-04-20T15:15:00Z",
        finished_at: "2026-04-20T15:16:00Z",
        decisions: [],
        orders: [],
      },
    });
    mockFetchAutoTradeRuns.mockResolvedValue([]);

    renderWithProviders(<AutoTradingPanel />);

    expect(await screen.findByText("$10.00")).toBeInTheDocument();
    expect(screen.getAllByText("$5.00").length).toBeGreaterThan(0);
    expect(screen.getByText(/Run #7/)).toBeInTheDocument();
    expect(mockFetchAutoTradingStatus).toHaveBeenCalledWith("secret");
  });
});
