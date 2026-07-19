import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { KalshiOrderDialog } from "@/components/trade/kalshi-order-dialog";
import { PriceDisplayProvider } from "@/lib/price-display";
import { renderWithProviders } from "@/test/render";

const { mockPlaceKalshiOrder, mockFetchTradingSettings } = vi.hoisted(() => ({
  mockPlaceKalshiOrder: vi.fn(),
  mockFetchTradingSettings: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    placeKalshiOrder: mockPlaceKalshiOrder,
    fetchTradingSettings: mockFetchTradingSettings,
  };
});

const DEFAULTS = {
  ticker: "KXNBAPTS-DAVION-10",
  side: "yes" as const,
  price: 0.4,
  displayLabel: "Davion Mitchell 10+ points",
  eventName: "Miami Heat at Toronto Raptors",
};

function renderDialog(environment: "live" | "demo" = "live") {
  return renderWithProviders(
    <PriceDisplayProvider initialMode="kalshi">
      <KalshiOrderDialog
        open
        onOpenChange={() => {}}
        defaults={DEFAULTS}
        environment={environment}
      />
    </PriceDisplayProvider>,
  );
}

beforeEach(() => {
  mockPlaceKalshiOrder.mockReset();
  mockFetchTradingSettings.mockReset();
  mockFetchTradingSettings.mockResolvedValue({ max_order_cost_dollars: 25 });
});

describe("KalshiOrderDialog", () => {
  it("walks form → confirm with cost, fee, payout, and env badge", async () => {
    const user = userEvent.setup();
    mockPlaceKalshiOrder.mockResolvedValue({ id: 1 });
    renderDialog("live");

    expect(screen.getByTestId("kalshi-order-env-badge")).toHaveTextContent("live · real money");

    await user.type(screen.getByTestId("kalshi-order-stake"), "10");
    // price prefilled from defaults (40¢) → 25 contracts, $10 cost
    expect(screen.getByTestId("kalshi-order-preview")).toHaveTextContent(
      "25 contracts · cost $10.00 · pays $25.00 if yes",
    );

    await user.click(screen.getByTestId("kalshi-order-review"));
    const summary = screen.getByTestId("kalshi-order-confirm-summary");
    expect(summary).toHaveTextContent("LIMIT YES @");
    expect(summary).toHaveTextContent("Total cost$10.00");
    // taker fee: ceil(0.07 × 25 × .4 × .6 × 100)/100 = $0.42
    expect(summary).toHaveTextContent("Est. fee (taker)$0.42");
    expect(summary).toHaveTextContent("Pays if it hits$25.00");
    expect(summary).toHaveTextContent("Per-order cap$25");

    await user.click(screen.getByTestId("kalshi-order-confirm"));
    await waitFor(() => expect(mockPlaceKalshiOrder).toHaveBeenCalledTimes(1));
    expect(mockPlaceKalshiOrder).toHaveBeenCalledWith({
      ticker: "KXNBAPTS-DAVION-10",
      side: "yes",
      action: "buy",
      quantity: 25,
      limit_price: 0.4,
      approved: true,
      time_in_force: "good_till_canceled",
    });
  });

  it("blocks review when the order exceeds the per-order cap", async () => {
    const user = userEvent.setup();
    renderDialog("live");

    await user.type(screen.getByTestId("kalshi-order-stake"), "40");
    await waitFor(() =>
      expect(screen.getByTestId("kalshi-order-cap-warning")).toHaveTextContent("$25 per-order cap"),
    );
    expect(screen.getByTestId("kalshi-order-review")).toBeDisabled();
    expect(mockPlaceKalshiOrder).not.toHaveBeenCalled();
  });

  it("labels the sandbox environment distinctly", async () => {
    renderDialog("demo");
    expect(screen.getByTestId("kalshi-order-env-badge")).toHaveTextContent("demo / sandbox");
  });

  it("surfaces server rejection inline and returns to the form", async () => {
    const user = userEvent.setup();
    mockPlaceKalshiOrder.mockRejectedValue(new Error("400 Order cost $40.00 exceeds the $25.00 per-order cap"));
    renderDialog("live");

    await user.type(screen.getByTestId("kalshi-order-stake"), "10");
    await user.click(screen.getByTestId("kalshi-order-review"));
    await user.click(screen.getByTestId("kalshi-order-confirm"));

    await waitFor(() =>
      expect(screen.getByText(/exceeds the \$25\.00 per-order cap/)).toBeInTheDocument(),
    );
    // back on the form stage for correction
    expect(screen.getByTestId("kalshi-order-stake")).toBeInTheDocument();
  });
});
