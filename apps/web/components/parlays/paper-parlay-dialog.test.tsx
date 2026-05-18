import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import { renderWithProviders } from "@/test/render";
import { PaperParlayDialog } from "./paper-parlay-dialog";
import { __testing, addLeg } from "./parlay-tray-store";
import type { TradeSelection } from "@/components/trade/trade-ticket";
import type { PaperParlayRead } from "@/lib/types";

const { mockOpenPaperParlay, mockMutate } = vi.hoisted(() => ({
  mockOpenPaperParlay: vi.fn(),
  mockMutate: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, openPaperParlay: mockOpenPaperParlay };
});

vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, mutate: mockMutate };
});

function makeLeg(ticker: string, overrides: Partial<TradeSelection> = {}): TradeSelection {
  return {
    kind: "player_prop",
    ticker,
    eventId: 1,
    marketTitle: `${ticker} 25+ points`,
    eventName: "Test Event",
    sportKey: "NBA",
    marketKind: "player_prop",
    displayLabel: `${ticker} 25+ points`,
    projectedSideLabel: null,
    selectedSide: "yes",
    selectedSideProbability: 0.6,
    entryPrice: 0.5,
    edge: 0.1,
    confidence: 0.8,
    kalshiUrl: null,
    subjectName: `Subject ${ticker}`,
    subjectTeam: "CLE",
    statKey: "points",
    threshold: 25,
    ...overrides,
  };
}

function paperParlayFixture(): PaperParlayRead {
  return {
    id: 1,
    created_at: "2026-05-17T20:00:00Z",
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
    legs: [],
  };
}

beforeEach(() => {
  __testing.reset();
  mockOpenPaperParlay.mockReset();
  mockMutate.mockReset();
});

describe("PaperParlayDialog", () => {
  it("renders the leg summary and live quote when open", () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    renderWithProviders(<PaperParlayDialog open onOpenChange={vi.fn()} />);

    const legs = screen.getByTestId("paper-parlay-dialog-legs");
    expect(legs.querySelectorAll("li")).toHaveLength(2);
    // Quote labels rendered for the operator to confirm the parlay
    // shape before saving.
    expect(screen.getByText(/combined price/i)).toBeInTheDocument();
    expect(screen.getByText(/american odds/i)).toBeInTheDocument();
    expect(screen.getByText(/model joint/i)).toBeInTheDocument();
    expect(screen.getByText(/^edge$/i)).toBeInTheDocument();
  });

  it("Save button stays disabled until a positive stake is entered", async () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    renderWithProviders(<PaperParlayDialog open onOpenChange={vi.fn()} />);

    const submit = screen.getByTestId("paper-parlay-dialog-submit");
    expect(submit).toBeDisabled();

    const user = userEvent.setup();
    await user.type(screen.getByTestId("paper-parlay-dialog-stake"), "0");
    expect(submit).toBeDisabled(); // 0 stake is still rejected.

    await user.clear(screen.getByTestId("paper-parlay-dialog-stake"));
    await user.type(screen.getByTestId("paper-parlay-dialog-stake"), "100");
    expect(submit).toBeEnabled();
  });

  it("shows the potential payout once a stake is typed", async () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    renderWithProviders(<PaperParlayDialog open onOpenChange={vi.fn()} />);

    const user = userEvent.setup();
    await user.type(screen.getByTestId("paper-parlay-dialog-stake"), "100");
    // combined = 0.5 * 0.5 = 0.25; payout = 100 * (1/0.25 - 1) = 300.
    expect(screen.getByTestId("paper-parlay-dialog-payout")).toHaveTextContent("300.00");
  });

  it("submits the locked-snapshot legs + stake on Save and closes the dialog", async () => {
    addLeg(makeLeg("A", { entryPrice: 0.5, selectedSide: "yes" }));
    addLeg(makeLeg("B", { entryPrice: 0.4, selectedSide: "no" }));
    mockOpenPaperParlay.mockResolvedValue(paperParlayFixture());
    const onOpenChange = vi.fn();
    renderWithProviders(<PaperParlayDialog open onOpenChange={onOpenChange} />);

    const user = userEvent.setup();
    await user.type(screen.getByTestId("paper-parlay-dialog-stake"), "100");
    await user.type(screen.getByTestId("paper-parlay-dialog-notes"), "test parlay");
    await user.click(screen.getByTestId("paper-parlay-dialog-submit"));

    await waitFor(() => expect(mockOpenPaperParlay).toHaveBeenCalledTimes(1));
    const payload = mockOpenPaperParlay.mock.calls[0][0];
    expect(payload.stake).toBe(100);
    expect(payload.notes).toBe("test parlay");
    expect(payload.legs).toEqual([
      { ticker: "A", side: "yes", suggested_price: 0.5 },
      { ticker: "B", side: "no", suggested_price: 0.4 },
    ]);
    // /positions invalidated so the portfolio table refetches.
    expect(mockMutate).toHaveBeenCalledWith("/positions");
    // Dialog closes on success.
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("displays the backend error message inline and keeps the dialog open on failure", async () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    mockOpenPaperParlay.mockRejectedValue(
      new Error("400 Market 'A' is not open for trading (status=settled)."),
    );
    const onOpenChange = vi.fn();
    renderWithProviders(<PaperParlayDialog open onOpenChange={onOpenChange} />);

    const user = userEvent.setup();
    await user.type(screen.getByTestId("paper-parlay-dialog-stake"), "50");
    await user.click(screen.getByTestId("paper-parlay-dialog-submit"));

    await waitFor(() => {
      const error = screen.getByTestId("paper-parlay-dialog-error");
      expect(error).toHaveTextContent(/not open for trading/i);
    });
    // Dialog stays open so the operator can read the error and try
    // again (e.g. remove the closed leg from the tray).
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("clears the tray on successful save", async () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    mockOpenPaperParlay.mockResolvedValue(paperParlayFixture());
    renderWithProviders(<PaperParlayDialog open onOpenChange={vi.fn()} />);

    const user = userEvent.setup();
    await user.type(screen.getByTestId("paper-parlay-dialog-stake"), "100");
    await user.click(screen.getByTestId("paper-parlay-dialog-submit"));

    await waitFor(() => expect(mockOpenPaperParlay).toHaveBeenCalled());
    // Re-render the dialog with the same store; the tray is empty.
    renderWithProviders(<PaperParlayDialog open onOpenChange={vi.fn()} />);
    const legs = screen.getAllByTestId("paper-parlay-dialog-legs");
    // Both rendered dialogs have empty leg lists now.
    for (const list of legs) {
      expect(list.querySelectorAll("li")).toHaveLength(0);
    }
  });
});
