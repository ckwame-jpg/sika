/** Price-product, odds, and payout math that is safe to keep client-side.
 * Correlation, joint probability, and edge are server-owned. */

import type { TradeSelection } from "@/components/trade/trade-ticket";

export interface PaperParlayQuote {
  legCount: number;
  combinedMarketPrice: number;
  americanOdds: string;
  /** ``stake * (1/combined_market_price - 1)`` — what the operator
   *  wins (profit, not total payout) on a successful settlement at
   *  the displayed combined price. ``null`` when stake is 0/missing
   *  or combinedMarketPrice is 0 (avoids division by zero). */
  potentialPayoutForStake: (stake: number) => number | null;
}

const EMPTY_QUOTE: PaperParlayQuote = {
  legCount: 0,
  combinedMarketPrice: 0,
  americanOdds: "+0",
  potentialPayoutForStake: () => null,
};

export function computePaperParlayQuote(
  legs: readonly TradeSelection[],
  serverCombinedMarketPrice?: number | null,
): PaperParlayQuote {
  if (legs.length === 0) return EMPTY_QUOTE;

  // Combined market price = product of operator's entry-price
  // snapshots (the values the operator saw when they added each leg).
  // Per decision #3, this is what gets locked into the saved parlay.
  const rawProduct = legs.reduce(
    (acc, leg) => acc * (leg.entryPrice ?? 0),
    1,
  );
  // The server's canonical six-decimal quote owns every displayed price and
  // downstream calculation once available.  ``toFixed(6)`` is the one
  // explicitly defined loading/error fallback; it avoids JavaScript's
  // ``Math.round(x * 1e6)`` half-tie drift from the Python quote engine.
  const fallbackCombinedMarketPrice = round6(rawProduct);
  const combinedMarketPrice =
    serverCombinedMarketPrice != null &&
    Number.isFinite(serverCombinedMarketPrice) &&
    serverCombinedMarketPrice > 0 &&
    serverCombinedMarketPrice <= 1
      ? serverCombinedMarketPrice
      : fallbackCombinedMarketPrice;

  return {
    legCount: legs.length,
    combinedMarketPrice,
    americanOdds: americanOddsFromProbability(combinedMarketPrice),
    potentialPayoutForStake: (stake: number) => {
      if (!stake || stake <= 0) return null;
      if (combinedMarketPrice <= 0) return null;
      return round2(stake * (1 / combinedMarketPrice - 1));
    },
  };
}

// Same formula as the backend's american_odds_from_probability
// (parlays.py:34). Duplicated so the tray doesn't need a network
// round-trip for what's a four-line clamp + ratio.
export function americanOddsFromProbability(probability: number): string {
  if (!Number.isFinite(probability) || probability <= 0) return "+0";
  const clamped = Math.max(0.01, Math.min(0.99, probability));
  const odds =
    clamped >= 0.5
      ? -Math.round((clamped / (1 - clamped)) * 100)
      : Math.round(((1 - clamped) / clamped) * 100);
  // Always sign-prefixed, no trailing decimals.
  return odds >= 0 ? `+${odds}` : `${odds}`;
}

function round6(value: number): number {
  return Number(value.toFixed(6));
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}
