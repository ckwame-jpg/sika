import { describe, expect, it } from "vitest";
import type { TradeSelection } from "@/components/trade/trade-ticket";
import { americanOddsFromProbability, computePaperParlayQuote } from "./paper-parlay-quote";

function makeLeg(overrides: Partial<TradeSelection> = {}): TradeSelection {
  return {
    kind: "player_prop",
    ticker: "LEG",
    eventId: 1,
    marketTitle: "Test market",
    eventName: "Event",
    sportKey: "NBA",
    marketKind: "player_prop",
    displayLabel: "Test market",
    projectedSideLabel: null,
    selectedSide: "yes",
    selectedSideProbability: 0.6,
    entryPrice: 0.5,
    edge: 0.1,
    confidence: 0.8,
    kalshiUrl: null,
    subjectName: "Player",
    subjectTeam: "CLE",
    statKey: "points",
    threshold: 25,
    ...overrides,
  };
}

describe("computePaperParlayQuote", () => {
  it("returns an empty quote for zero legs", () => {
    const quote = computePaperParlayQuote([]);
    expect(quote.legCount).toBe(0);
    expect(quote.combinedMarketPrice).toBe(0);
    expect(quote.potentialPayoutForStake(100)).toBeNull();
  });

  it("combined_market_price is the product of leg entry-price snapshots", () => {
    const quote = computePaperParlayQuote([
      makeLeg({ ticker: "A", entryPrice: 0.5, selectedSideProbability: 0.6 }),
      makeLeg({ ticker: "B", entryPrice: 0.4, selectedSideProbability: 0.5 }),
    ]);
    // 0.5 * 0.4 = 0.20
    expect(quote.combinedMarketPrice).toBeCloseTo(0.2, 6);
  });

  it("uses six-decimal pricing and the server price for payout math", () => {
    const legs = [
      makeLeg({ ticker: "A", entryPrice: 0.10 }),
      makeLeg({ ticker: "B", entryPrice: 0.15 }),
      makeLeg({ ticker: "C", entryPrice: 0.15 }),
      makeLeg({ ticker: "D", entryPrice: 0.25 }),
    ];
    expect(computePaperParlayQuote(legs).combinedMarketPrice).toBe(0.000562);

    const serverQuote = computePaperParlayQuote(legs, 0.000561);
    expect(serverQuote.combinedMarketPrice).toBe(0.000561);
    expect(serverQuote.potentialPayoutForStake(100)).toBeCloseTo(
      100 * (1 / 0.000561 - 1),
      2,
    );
  });

  it("does not duplicate server-owned joint probability or edge math", () => {
    const quote = computePaperParlayQuote([
      makeLeg({ ticker: "A" }),
      makeLeg({ ticker: "B" }),
    ]);
    expect(quote).not.toHaveProperty("combinedModelProbability");
    expect(quote).not.toHaveProperty("edge");
  });

  it("potentialPayoutForStake returns stake * (1/combined - 1) on a positive stake", () => {
    const quote = computePaperParlayQuote([
      makeLeg({ ticker: "A", entryPrice: 0.5, selectedSideProbability: 0.6 }),
      makeLeg({ ticker: "B", entryPrice: 0.5, selectedSideProbability: 0.5 }),
    ]);
    // combined = 0.25; payout on $100 = 100 * (1/0.25 - 1) = $300.
    expect(quote.potentialPayoutForStake(100)).toBeCloseTo(300, 2);
  });

  it("potentialPayoutForStake returns null for non-positive stake", () => {
    const quote = computePaperParlayQuote([
      makeLeg({ ticker: "A" }),
      makeLeg({ ticker: "B" }),
    ]);
    expect(quote.potentialPayoutForStake(0)).toBeNull();
    expect(quote.potentialPayoutForStake(-50)).toBeNull();
  });
});

describe("americanOddsFromProbability", () => {
  it("matches the backend formula at common breakpoints", () => {
    // P = 0.5 → -100 (sign-prefixed); but backend rounds to -100 exactly.
    expect(americanOddsFromProbability(0.5)).toBe("-100");
    // P = 0.6 (favorite) → -150
    expect(americanOddsFromProbability(0.6)).toBe("-150");
    // P = 0.4 (underdog) → +150
    expect(americanOddsFromProbability(0.4)).toBe("+150");
    // P = 0.25 → +300
    expect(americanOddsFromProbability(0.25)).toBe("+300");
  });

  it("clamps to (0.01, 0.99) so edge cases don't blow up", () => {
    // 0 and 1 are invalid; should be treated as the clamped bounds.
    expect(americanOddsFromProbability(0)).toBe("+0");
    expect(americanOddsFromProbability(1)).toMatch(/^-/);
    expect(americanOddsFromProbability(-1)).toBe("+0");
  });
});
