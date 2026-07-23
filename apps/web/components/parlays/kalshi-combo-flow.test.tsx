/**
 * Tray combinability row + KalshiComboDialog flow — the real-parlay
 * exit from the paper tray. previewKalshiCombo is debounced 500ms in
 * the tray, so assertions use generous waitFor timeouts.
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TradeSelection } from "@/components/trade/trade-ticket";
import { ParlayTray } from "./parlay-tray";
import { __testing, addLeg } from "./parlay-tray-store";
import { PriceDisplayProvider } from "@/lib/price-display";
import { renderWithProviders } from "@/test/render";

const {
  mockFetchMyKalshiCredentials,
  mockPreviewKalshiCombo,
  mockPlaceKalshiCombo,
  mockFetchTradingSettings,
} = vi.hoisted(() => ({
  mockFetchMyKalshiCredentials: vi.fn(),
  mockPreviewKalshiCombo: vi.fn(),
  mockPlaceKalshiCombo: vi.fn(),
  mockFetchTradingSettings: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchMyKalshiCredentials: mockFetchMyKalshiCredentials,
    previewKalshiCombo: mockPreviewKalshiCombo,
    placeKalshiCombo: mockPlaceKalshiCombo,
    fetchTradingSettings: mockFetchTradingSettings,
  };
});

function makeLeg(ticker: string, overrides: Partial<TradeSelection> = {}): TradeSelection {
  return {
    kind: "player_prop",
    ticker,
    eventId: 1,
    marketTitle: `${ticker} prop`,
    eventName: "Test Event",
    sportKey: "MLB",
    marketKind: "player_prop",
    displayLabel: `${ticker} prop`,
    projectedSideLabel: null,
    selectedSide: "yes",
    selectedSideProbability: 0.6,
    entryPrice: 0.5,
    edge: 0.1,
    confidence: 0.8,
    kalshiUrl: null,
    subjectName: `Subject ${ticker}`,
    subjectTeam: "NYY",
    statKey: "points",
    threshold: 25,
    ...overrides,
  };
}

const COMBINABLE_PREVIEW = {
  combinable: true,
  reason: null,
  collection_ticker: "KXMLBCOMBO",
  existing_market_ticker: "KXCOMBO-EXISTING",
  implied_price: 0.25,
  quote_yes_bid: 0.18,
  quote_yes_ask: 0.22,
};

function renderTray() {
  return renderWithProviders(
    <PriceDisplayProvider initialMode="kalshi">
      <ParlayTray onSave={() => {}} />
    </PriceDisplayProvider>,
  );
}

beforeEach(() => {
  __testing.reset();
  mockFetchMyKalshiCredentials.mockReset();
  mockPreviewKalshiCombo.mockReset();
  mockPlaceKalshiCombo.mockReset();
  mockFetchTradingSettings.mockReset();
  mockFetchMyKalshiCredentials.mockResolvedValue({
    configured: true,
    key_id: "k1",
    base_url: "https://demo-api.kalshi.co/trade-api/v2",
    updated_at: null,
  });
  mockFetchTradingSettings.mockResolvedValue({ max_order_cost_dollars: 25 });
});

describe("parlay tray → kalshi combo flow", () => {
  it("shows combinability and places a real combo, clearing the tray", async () => {
    const user = userEvent.setup();
    mockPreviewKalshiCombo.mockResolvedValue(COMBINABLE_PREVIEW);
    mockPlaceKalshiCombo.mockResolvedValue({ id: 42 });

    addLeg(makeLeg("KXA"));
    addLeg(makeLeg("KXB", { selectedSide: "no", entryPrice: 0.6 }));
    renderTray();

    // Debounced preview lands → status row + enabled live button.
    await waitFor(
      () =>
        expect(screen.getByTestId("parlay-tray-combinability")).toHaveTextContent(
          "combinable on kalshi ✓ · live combo market exists · ask 22¢",
        ),
      { timeout: 3000 },
    );
    expect(mockPreviewKalshiCombo).toHaveBeenCalledWith({
      legs: [
        expect.objectContaining({ ticker: "KXA", side: "yes", entry_price: 0.5 }),
        expect.objectContaining({ ticker: "KXB", side: "no", entry_price: 0.6 }),
      ],
    });

    const placeButton = screen.getByTestId("parlay-tray-place-kalshi");
    expect(placeButton).toBeEnabled();
    expect(placeButton).toHaveTextContent("place combo on kalshi · demo");
    await user.click(placeButton);

    // Dialog: legs listed; fill-now prefills live ask 22¢ + 3¢ buffer.
    expect(screen.getByTestId("kalshi-combo-legs")).toHaveTextContent("KXA prop");
    expect(screen.getByTestId("kalshi-combo-price")).toHaveValue("25");

    await user.type(screen.getByTestId("kalshi-combo-stake"), "5");
    // $5 @ max 25¢ → 20 contracts
    await waitFor(() =>
      expect(screen.getByTestId("kalshi-combo-preview-line")).toHaveTextContent(
        "20 contracts",
      ),
    );

    await user.click(screen.getByTestId("kalshi-combo-review"));
    expect(screen.getByTestId("kalshi-combo-confirm-summary")).toHaveTextContent(
      "Pays if ALL legs hit$20.00",
    );

    await user.click(screen.getByTestId("kalshi-combo-confirm"));
    await waitFor(() => expect(mockPlaceKalshiCombo).toHaveBeenCalledTimes(1));
    expect(mockPlaceKalshiCombo).toHaveBeenCalledWith({
      legs: [
        expect.objectContaining({ ticker: "KXA", side: "yes" }),
        expect.objectContaining({ ticker: "KXB", side: "no" }),
      ],
      quantity: 20,
      limit_price: 0.25,
      approved: true,
      time_in_force: "immediate_or_cancel",
    });

    // Success clears the tray (legs are now a real order).
    await waitFor(() => expect(screen.queryByTestId("parlay-tray")).not.toBeInTheDocument());
  });

  it("shows the server reason and disables the live button when not combinable", async () => {
    mockPreviewKalshiCombo.mockResolvedValue({
      combinable: false,
      reason: "only 1 leg(s) allowed from EV-NYY",
      collection_ticker: null,
      existing_market_ticker: null,
      implied_price: null,
      quote_yes_bid: null,
      quote_yes_ask: null,
    });

    addLeg(makeLeg("KXA"));
    addLeg(makeLeg("KXB"));
    renderTray();

    await waitFor(
      () =>
        expect(screen.getByTestId("parlay-tray-combinability")).toHaveTextContent(
          "only 1 leg(s) allowed from EV-NYY",
        ),
      { timeout: 3000 },
    );
    expect(screen.getByTestId("parlay-tray-place-kalshi")).toBeDisabled();
  });

  it("blocks a combo when principal fits but its fee exceeds the cap", async () => {
    const user = userEvent.setup();
    mockPreviewKalshiCombo.mockResolvedValue(COMBINABLE_PREVIEW);
    mockFetchTradingSettings.mockResolvedValue({ max_order_cost_dollars: 5 });

    addLeg(makeLeg("KXA"));
    addLeg(makeLeg("KXB"));
    renderTray();

    const placeButton = await screen.findByTestId(
      "parlay-tray-place-kalshi",
      {},
      { timeout: 3000 },
    );
    await waitFor(() => expect(placeButton).toBeEnabled(), { timeout: 3000 });
    await user.click(placeButton);
    await user.type(screen.getByTestId("kalshi-combo-stake"), "5");

    // 20 @ 25¢ has exactly $5 principal, but a 27¢ worst-case fee.
    expect(screen.getByTestId("kalshi-combo-preview-line")).toHaveTextContent("cost $5.00");
    await waitFor(() =>
      expect(screen.getByTestId("kalshi-combo-cap-warning")).toHaveTextContent(
        "Principal plus worst-case taker fee",
      ),
    );
    expect(screen.getByTestId("kalshi-combo-review")).toBeDisabled();
    expect(mockPlaceKalshiCombo).not.toHaveBeenCalled();
  });

  it("allows a combo when one 40-cent contract plus its 2-cent fee equals the cap", async () => {
    const user = userEvent.setup();
    mockPreviewKalshiCombo.mockResolvedValue({
      ...COMBINABLE_PREVIEW,
      quote_yes_ask: 0.37,
    });
    mockFetchTradingSettings.mockResolvedValue({ max_order_cost_dollars: 0.42 });

    addLeg(makeLeg("KXA"));
    addLeg(makeLeg("KXB"));
    renderTray();

    const placeButton = await screen.findByTestId(
      "parlay-tray-place-kalshi",
      {},
      { timeout: 3000 },
    );
    await waitFor(() => expect(placeButton).toBeEnabled(), { timeout: 3000 });
    await user.click(placeButton);
    await user.type(screen.getByTestId("kalshi-combo-stake"), "0.40");

    expect(screen.getByTestId("kalshi-combo-preview-line")).toHaveTextContent(
      "1 contract · cost $0.40",
    );
    await waitFor(() => expect(mockFetchTradingSettings).toHaveBeenCalled());
    expect(screen.queryByTestId("kalshi-combo-cap-warning")).not.toBeInTheDocument();
    expect(screen.getByTestId("kalshi-combo-review")).toBeEnabled();
  });

  it("hides the combo affordance entirely without credentials", async () => {
    mockFetchMyKalshiCredentials.mockResolvedValue({ configured: false });
    addLeg(makeLeg("KXA"));
    addLeg(makeLeg("KXB"));
    renderTray();

    await waitFor(() => expect(mockFetchMyKalshiCredentials).toHaveBeenCalled());
    expect(screen.queryByTestId("parlay-tray-place-kalshi")).not.toBeInTheDocument();
    expect(screen.queryByTestId("parlay-tray-combinability")).not.toBeInTheDocument();
    expect(mockPreviewKalshiCombo).not.toHaveBeenCalled();
  });
});
