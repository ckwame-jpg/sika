import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import type { TradeSelection } from "@/components/trade/trade-ticket";
import { ParlayTray } from "./parlay-tray";
import { __testing, addLeg } from "./parlay-tray-store";

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
});

describe("ParlayTray", () => {
  it("renders nothing when the tray is empty", () => {
    const { container } = render(<ParlayTray />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a chip per leg with the live quote stats", () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    render(<ParlayTray />);
    expect(screen.getByTestId("parlay-tray")).toBeInTheDocument();
    const chips = screen.getByTestId("parlay-tray-chips").querySelectorAll("li");
    expect(chips).toHaveLength(2);
    // Quote stats appear (label-only checks; numeric values are tested
    // exhaustively in paper-parlay-quote.test.ts).
    const quote = screen.getByTestId("parlay-tray-quote");
    expect(quote).toHaveTextContent(/combined/i);
    expect(quote).toHaveTextContent(/odds/i);
    expect(quote).toHaveTextContent(/joint prob/i);
    expect(quote).toHaveTextContent(/edge/i);
  });

  it("Save button is disabled with < 2 legs and re-labels accordingly", () => {
    addLeg(makeLeg("ONLY"));
    render(<ParlayTray onSave={vi.fn()} />);
    const save = screen.getByTestId("parlay-tray-save");
    expect(save).toBeDisabled();
    expect(save).toHaveTextContent(/add another leg/i);
  });

  it("Save button enables with >= 2 legs and an onSave callback", () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    const onSave = vi.fn();
    render(<ParlayTray onSave={onSave} />);
    const save = screen.getByTestId("parlay-tray-save");
    expect(save).toBeEnabled();
    expect(save).toHaveTextContent(/save paper parlay/i);
  });

  it("Save button stays disabled when no onSave is passed (step 5 default)", () => {
    // Step 5 ships the tray ahead of step 6's dialog; the parent
    // doesn't pass onSave yet, so the button is intentionally inert.
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    render(<ParlayTray />);
    expect(screen.getByTestId("parlay-tray-save")).toBeDisabled();
  });

  it("removing a chip drops the leg from the tray", async () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    render(<ParlayTray />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("parlay-tray-chip-remove-A"));
    const remainingChips = screen.getByTestId("parlay-tray-chips").querySelectorAll("li");
    expect(remainingChips).toHaveLength(1);
    expect(screen.queryByTestId("parlay-tray-chip-remove-A")).toBeNull();
  });

  it("clear empties the tray and the tray returns null on next render", async () => {
    addLeg(makeLeg("A"));
    addLeg(makeLeg("B"));
    const { container } = render(<ParlayTray />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("parlay-tray-clear"));
    expect(container.firstChild).toBeNull();
  });
});
