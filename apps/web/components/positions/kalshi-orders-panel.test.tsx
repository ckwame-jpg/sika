import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { KalshiOrdersPanel } from "@/components/positions/kalshi-orders-panel";
import { PriceDisplayProvider } from "@/lib/price-display";
import { renderWithProviders } from "@/test/render";

function renderPanel() {
  return renderWithProviders(
    <PriceDisplayProvider initialMode="cents">
      <KalshiOrdersPanel />
    </PriceDisplayProvider>,
  );
}

const {
  mockFetchKalshiOrders,
  mockCancelKalshiOrder,
  mockFetchMyKalshiCredentials,
} = vi.hoisted(() => ({
  mockFetchKalshiOrders: vi.fn(),
  mockCancelKalshiOrder: vi.fn(),
  mockFetchMyKalshiCredentials: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchKalshiOrders: mockFetchKalshiOrders,
    cancelKalshiOrder: mockCancelKalshiOrder,
    fetchMyKalshiCredentials: mockFetchMyKalshiCredentials,
  };
});

const RESTING_ORDER = {
  id: 7,
  kind: "single",
  ticker: "KXNBAPTS-DAVION-10",
  environment: "live",
  client_order_id: "c-1",
  kalshi_order_id: "k-1",
  side: "yes",
  action: "buy",
  quantity: 25,
  limit_price: 0.4,
  status: "resting",
  collection_ticker: null,
  combo_event_ticker: null,
  approved_by_user: true,
  error_detail: null,
  created_at: "2026-07-19T18:00:00Z",
  submitted_at: "2026-07-19T18:00:01Z",
  last_synced_at: null,
  legs: [],
  fills: [{ id: 1, kalshi_fill_id: "f1", count: 10, price: 0.4, side: "yes", fee_dollars: 0.17, created_at: "2026-07-19T18:01:00Z" }],
};

beforeEach(() => {
  mockFetchKalshiOrders.mockReset();
  mockCancelKalshiOrder.mockReset();
  mockFetchMyKalshiCredentials.mockReset();
  mockFetchMyKalshiCredentials.mockResolvedValue({
    configured: true,
    key_id: "k1",
    base_url: "https://api.elections.kalshi.com/trade-api/v2",
    updated_at: null,
  });
});

describe("KalshiOrdersPanel", () => {
  it("renders nothing until credentials are connected", async () => {
    mockFetchMyKalshiCredentials.mockResolvedValue({ configured: false });
    renderPanel();
    await waitFor(() => expect(mockFetchMyKalshiCredentials).toHaveBeenCalled());
    expect(screen.queryByTestId("kalshi-orders-panel")).not.toBeInTheDocument();
    expect(mockFetchKalshiOrders).not.toHaveBeenCalled();
  });

  it("lists orders with env + status and cancels a resting order", async () => {
    const user = userEvent.setup();
    mockFetchKalshiOrders.mockResolvedValue([RESTING_ORDER]);
    mockCancelKalshiOrder.mockResolvedValue({ ...RESTING_ORDER, status: "cancelling" });

    renderPanel();
    const row = await screen.findByTestId("kalshi-order-row");
    expect(row).toHaveTextContent("KXNBAPTS-DAVION-10");
    expect(row).toHaveTextContent("live");
    expect(row).toHaveTextContent("filled 10/25");
    expect(screen.getByTestId("kalshi-order-status")).toHaveTextContent("resting");

    await user.click(screen.getByTestId("kalshi-order-cancel"));
    await waitFor(() => expect(mockCancelKalshiOrder).toHaveBeenCalledWith(7));
  });

  it("surfaces failed submissions with the error detail", async () => {
    mockFetchKalshiOrders.mockResolvedValue([
      {
        ...RESTING_ORDER,
        id: 8,
        status: "submission_failed",
        kalshi_order_id: null,
        error_detail: "Kalshi rejected the order (400): insufficient balance",
        fills: [],
      },
    ]);
    renderPanel();
    const row = await screen.findByTestId("kalshi-order-row");
    expect(row).toHaveTextContent("insufficient balance");
    expect(screen.getByTestId("kalshi-order-status")).toHaveTextContent("submission failed");
    // terminal + no kalshi id → no cancel affordance
    expect(screen.queryByTestId("kalshi-order-cancel")).not.toBeInTheDocument();
  });

  it("shows the empty state when connected with no orders", async () => {
    mockFetchKalshiOrders.mockResolvedValue([]);
    renderPanel();
    expect(await screen.findByTestId("kalshi-orders-empty")).toHaveTextContent(
      "No real orders yet",
    );
  });
});
