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
    expect(quote.combinedModelProbability).toBe(0);
    expect(quote.edge).toBe(0);
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

  it("combined_model_probability is the strict product when legs share no correlation", () => {
    // Different subjects, different teams.
    const quote = computePaperParlayQuote([
      makeLeg({
        ticker: "A",
        subjectName: "Alpha",
        subjectTeam: "AAA",
        selectedSideProbability: 0.6,
        entryPrice: 0.5,
      }),
      makeLeg({
        ticker: "B",
        subjectName: "Beta",
        subjectTeam: "BBB",
        selectedSideProbability: 0.5,
        entryPrice: 0.4,
      }),
    ]);
    // 0.6 * 0.5 = 0.30
    expect(quote.combinedModelProbability).toBeCloseTo(0.3, 6);
  });

  it("lifts combined_model_probability for shared-subject legs (pins to backend formula)", () => {
    // Two legs on the same player. Same expected value as the backend:
    // 0.30 + 0.70 * (0.50 - 0.30) = 0.44.
    const quote = computePaperParlayQuote([
      makeLeg({
        ticker: "A",
        subjectName: "Same Player",
        subjectTeam: "CLE",
        selectedSideProbability: 0.6,
        entryPrice: 0.5,
      }),
      makeLeg({
        ticker: "B",
        subjectName: "Same Player",
        subjectTeam: "CLE",
        selectedSideProbability: 0.5,
        entryPrice: 0.45,
      }),
    ]);
    expect(quote.combinedModelProbability).toBeCloseTo(0.44, 6);
    // Edge = 0.44 - (0.5 * 0.45) = 0.44 - 0.225 = 0.215
    expect(quote.edge).toBeCloseTo(0.215, 6);
  });

  it("lifts (less) for same-team legs without shared subject", () => {
    // Same team but different subjects → weight 0.3, 1 pair, 1 total pair.
    // independent = 0.6 * 0.5 = 0.30, min = 0.5, correlation = 0.3.
    // joint = 0.30 + 0.30 * (0.50 - 0.30) = 0.36.
    const quote = computePaperParlayQuote([
      makeLeg({
        ticker: "A",
        subjectName: "Player A",
        subjectTeam: "CLE",
        selectedSideProbability: 0.6,
        entryPrice: 0.5,
      }),
      makeLeg({
        ticker: "B",
        subjectName: "Player B",
        subjectTeam: "CLE",
        selectedSideProbability: 0.5,
        entryPrice: 0.45,
      }),
    ]);
    expect(quote.combinedModelProbability).toBeCloseTo(0.36, 6);
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
