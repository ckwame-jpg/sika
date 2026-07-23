import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import type { TradeSelection } from "@/components/trade/trade-ticket";
import { renderWithProviders } from "@/test/render";
import { ParlayTray } from "./parlay-tray";
import { __testing, addLeg } from "./parlay-tray-store";

const { mockQuotePaperParlay } = vi.hoisted(() => ({
  mockQuotePaperParlay: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, quotePaperParlay: mockQuotePaperParlay };
});

function makeLeg(ticker: string, overrides: Partial<TradeSelection> = {}): TradeSelection {
  return {
    kind: "player_prop",
    ticker,
    eventId: 1,
    marketTitle: `${ticker} prop`,
    eventName: "Test Event",
    sportKey: "NBA",
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
    subjectTeam: "CLE",
    statKey: "points",
    threshold: 25,
    ...overrides,
  };
}

beforeEach(() => {
  __testing.reset();
  mockQuotePaperParlay.mockReset();
  mockQuotePaperParlay.mockResolvedValue({
    combined_market_price: 0.25,
    joint_probability: 0.44,
    edge: 0.19,
    pair_counts: {
      shared_subject: 1,
      qb_receiver_stack: 0,
      player_team_total: 0,
      same_team: 0,
      shared_opponent: 0,
    },
    correlation_factor: 0.7,
  });
});

describe("ParlayTray", () => {
  it("renders nothing when the tray is empty", () => {
    const { container } = renderWithProviders(<ParlayTray />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a chip per leg with the live quote stats", () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    renderWithProviders(<ParlayTray />);
    expect(screen.getByTestId("parlay-tray")).toBeInTheDocument();
    const chips = screen.getByTestId("parlay-tray-chips").querySelectorAll("li");
    expect(chips).toHaveLength(2);
    // Quote stats appear while the server quote resolves.
    const quote = screen.getByTestId("parlay-tray-quote");
    expect(quote).toHaveTextContent(/combined/i);
    expect(quote).toHaveTextContent(/odds/i);
    expect(quote).toHaveTextContent(/joint prob/i);
    expect(quote).toHaveTextContent(/edge/i);
  });

  it("renders joint probability and edge from the server quote", async () => {
    addLeg(makeLeg("A", { selectedSide: "yes", entryPrice: 0.5 }));
    addLeg(makeLeg("B", { selectedSide: "no", entryPrice: 0.5 }));
    renderWithProviders(<ParlayTray />);

    await waitFor(() =>
      expect(screen.getByTestId("parlay-tray-quote")).toHaveTextContent("44.0%"),
    );
    expect(screen.getByTestId("parlay-tray-quote")).toHaveTextContent("+19.0%");
    expect(mockQuotePaperParlay).toHaveBeenCalledWith({
      legs: [
        { ticker: "A", side: "yes", suggested_price: 0.5 },
        { ticker: "B", side: "no", suggested_price: 0.5 },
      ],
    });
  });

  it("uses the server combined price for displayed odds and projection", async () => {
    mockQuotePaperParlay.mockResolvedValue({
      combined_market_price: 0.20,
      joint_probability: 0.44,
      edge: 0.24,
      pair_counts: {
        shared_subject: 0,
        qb_receiver_stack: 0,
        player_team_total: 0,
        same_team: 1,
        shared_opponent: 0,
      },
      correlation_factor: 0.3,
    });
    addLeg(makeLeg("A", { entryPrice: 0.5 }));
    addLeg(makeLeg("B", { entryPrice: 0.5 }));
    renderWithProviders(<ParlayTray />);

    await waitFor(() => {
      const quote = screen.getByTestId("parlay-tray-quote");
      expect(quote).toHaveTextContent("0.20");
      expect(quote).toHaveTextContent("+400");
    });
    const user = userEvent.setup();
    await user.type(screen.getByTestId("parlay-tray-stake"), "100");
    expect(screen.getByTestId("parlay-tray-projection")).toHaveTextContent(
      "$500.00",
    );
  });

  it("Save button is disabled with < 2 legs and re-labels accordingly", () => {
    addLeg(makeLeg("ONLY"));
    renderWithProviders(<ParlayTray onSave={vi.fn()} />);
    const save = screen.getByTestId("parlay-tray-save");
    expect(save).toBeDisabled();
    expect(save).toHaveTextContent(/add another leg/i);
  });

  it("Save button enables with >= 2 legs and an onSave callback", () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    const onSave = vi.fn();
    renderWithProviders(<ParlayTray onSave={onSave} />);
    const save = screen.getByTestId("parlay-tray-save");
    expect(save).toBeEnabled();
    expect(save).toHaveTextContent(/save paper parlay/i);
  });

  it("Save button stays disabled when no onSave is passed (step 5 default)", () => {
    // Step 5 ships the tray ahead of step 6's dialog; the parent
    // doesn't pass onSave yet, so the button is intentionally inert.
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    renderWithProviders(<ParlayTray />);
    expect(screen.getByTestId("parlay-tray-save")).toBeDisabled();
  });

  it("removing a chip drops the leg from the tray", async () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    renderWithProviders(<ParlayTray />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("parlay-tray-chip-remove-A"));
    const remainingChips = screen.getByTestId("parlay-tray-chips").querySelectorAll("li");
    expect(remainingChips).toHaveLength(1);
    expect(screen.queryByTestId("parlay-tray-chip-remove-A")).toBeNull();
  });

  it("clear empties the tray and the tray returns null on next render", async () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    const { container } = renderWithProviders(<ParlayTray />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("parlay-tray-clear"));
    expect(container.firstChild).toBeNull();
  });
});
