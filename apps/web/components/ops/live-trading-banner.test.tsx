import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LiveTradingBanner } from "@/components/ops/live-trading-banner";
import { renderWithProviders } from "@/test/render";

const { mockDisableAutoTrading, mockEnableAutoTrading, mockFetchAutoTradingStatus } = vi.hoisted(() => ({
  mockDisableAutoTrading: vi.fn(),
  mockEnableAutoTrading: vi.fn(),
  mockFetchAutoTradingStatus: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    disableAutoTrading: mockDisableAutoTrading,
    enableAutoTrading: mockEnableAutoTrading,
    fetchAutoTradingStatus: mockFetchAutoTradingStatus,
  };
});

describe("LiveTradingBanner", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("stays hidden until owner access is available", () => {
    renderWithProviders(<LiveTradingBanner />);

    expect(screen.queryByText("Live Trading ON")).not.toBeInTheDocument();
    expect(mockFetchAutoTradingStatus).not.toHaveBeenCalled();
  });

  it("shows live trading status and can trigger the kill switch", async () => {
    window.localStorage.setItem("sika_owner_admin_token", "secret");
    mockFetchAutoTradingStatus.mockResolvedValue({
      enabled_by_env: true,
      kill_switch_active: false,
      effective_enabled: true,
      daily_budget_cents: 1000,
      spent_today_cents: 250,
      remaining_budget_cents: 750,
      max_orders_per_day: 5,
      local_trade_date: "2026-04-20",
      local_run_time: "10:15",
      market_scope: "nba_mlb_current_slate",
      allow_parlays: false,
      live_credentials_configured: true,
      latest_run: null,
      latest_account_snapshot: null,
    });
    mockDisableAutoTrading.mockResolvedValue({
      enabled_by_env: true,
      kill_switch_active: true,
      effective_enabled: false,
      daily_budget_cents: 1000,
      spent_today_cents: 250,
      remaining_budget_cents: 750,
      max_orders_per_day: 5,
      local_trade_date: "2026-04-20",
      local_run_time: "10:15",
      market_scope: "nba_mlb_current_slate",
      allow_parlays: false,
      live_credentials_configured: true,
      latest_run: null,
      latest_account_snapshot: null,
    });

    renderWithProviders(<LiveTradingBanner />);

    expect(await screen.findByText("Live Trading ON")).toBeInTheDocument();
    expect(screen.getByText(/Remaining today/)).toHaveTextContent("$7.50");

    fireEvent.click(screen.getByRole("button", { name: "Disable" }));

    await waitFor(() => expect(mockDisableAutoTrading).toHaveBeenCalledWith("secret"));
  });

  it("shows an enable action when the kill switch is active", async () => {
    window.localStorage.setItem("sika_owner_admin_token", "secret");
    mockFetchAutoTradingStatus.mockResolvedValue({
      enabled_by_env: true,
      kill_switch_active: true,
      effective_enabled: false,
      daily_budget_cents: 1000,
      spent_today_cents: 0,
      remaining_budget_cents: 1000,
      max_orders_per_day: 5,
      local_trade_date: "2026-04-20",
      local_run_time: "10:15",
      market_scope: "nba_mlb_current_slate",
      allow_parlays: false,
      live_credentials_configured: true,
      latest_run: null,
      latest_account_snapshot: null,
    });
    mockEnableAutoTrading.mockResolvedValue({
      enabled_by_env: true,
      kill_switch_active: false,
      effective_enabled: true,
      daily_budget_cents: 1000,
      spent_today_cents: 0,
      remaining_budget_cents: 1000,
      max_orders_per_day: 5,
      local_trade_date: "2026-04-20",
      local_run_time: "10:15",
      market_scope: "nba_mlb_current_slate",
      allow_parlays: false,
      live_credentials_configured: true,
      latest_run: null,
      latest_account_snapshot: null,
    });

    renderWithProviders(<LiveTradingBanner />);

    expect(await screen.findByText("Live Trading Disabled")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Enable" }));

    await waitFor(() => expect(mockEnableAutoTrading).toHaveBeenCalledWith("secret"));
  });
});
