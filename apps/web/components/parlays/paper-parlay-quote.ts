/**
 * Client-side joint-probability + payout math for the parlay tray.
 *
 * Mirrors the BACKEND formula in
 * ``apps/api/app/services/paper_parlays.py:_correlation_adjusted_joint``
 * so the tray's live preview agrees with what the backend will save.
 * If the backend formula moves, the regression test in
 * ``paper-parlay-quote.test.ts`` will catch the drift loudly.
 *
 * Why client-side: the tray re-renders on every add/remove and on
 * every entry-price snapshot input. A backend round-trip per
 * keystroke would make the UX feel laggy. The values shown are
 * advisory; the AUTHORITATIVE quote lands when the operator hits
 * Save (step 6) — and the locked snapshot the operator sees IS
 * the suggested_price they typed into the tray, so there's no
 * round-trip needed for that field at all.
 */

import type { TradeSelection } from "@/components/trade/trade-ticket";

// Same pair weights / cap as the backend. Duplicated here rather
// than imported via a contracts file because the math is small,
// stable, and self-contained — and the regression test pins both
// values so a divergence fails at PR review.
const PAIR_WEIGHT_SHARED_SUBJECT = 0.7;
const PAIR_WEIGHT_SAME_TEAM = 0.3;
const PAIR_WEIGHT_SHARED_OPPONENT = 0.2;
const CORRELATION_CAP = 0.85;

export interface PaperParlayQuote {
  legCount: number;
  combinedMarketPrice: number;
  combinedModelProbability: number;
  edge: number;
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
  combinedModelProbability: 0,
  edge: 0,
  americanOdds: "+0",
  potentialPayoutForStake: () => null,
};

export function computePaperParlayQuote(
  legs: readonly TradeSelection[],
): PaperParlayQuote {
  if (legs.length === 0) return EMPTY_QUOTE;

  // Combined market price = product of operator's entry-price
  // snapshots (the values the operator saw when they added each leg).
  // Per decision #3, this is what gets locked into the saved parlay.
  const combinedMarketPrice = legs.reduce(
    (acc, leg) => acc * (leg.entryPrice ?? 0),
    1,
  );

  const legProbs = legs.map((leg) => Number(leg.selectedSideProbability ?? 0));
  const independent = legProbs.reduce((acc, prob) => acc * prob, 1);

  let combinedModelProbability = independent;
  if (legs.length > 1) {
    const minLeg = Math.min(...legProbs);
    const pairs = countCorrelationPairs(legs);
    const totalPairs = (legs.length * (legs.length - 1)) / 2;
    const weighted =
      (PAIR_WEIGHT_SHARED_SUBJECT * pairs.sharedSubject +
        PAIR_WEIGHT_SAME_TEAM * pairs.sameTeam +
        PAIR_WEIGHT_SHARED_OPPONENT * pairs.sharedOpponent) /
      Math.max(totalPairs, 1);
    const correlationFactor = Math.min(weighted, CORRELATION_CAP);
    combinedModelProbability =
      independent + correlationFactor * (minLeg - independent);
  }

  const edge = combinedModelProbability - combinedMarketPrice;

  return {
    legCount: legs.length,
    combinedMarketPrice: round6(combinedMarketPrice),
    combinedModelProbability: round6(combinedModelProbability),
    edge: round6(edge),
    americanOdds: americanOddsFromProbability(combinedMarketPrice),
    potentialPayoutForStake: (stake: number) => {
      if (!stake || stake <= 0) return null;
      if (combinedMarketPrice <= 0) return null;
      return round2(stake * (1 / combinedMarketPrice - 1));
    },
  };
}

function countCorrelationPairs(legs: readonly TradeSelection[]): {
  sharedSubject: number;
  sameTeam: number;
  sharedOpponent: number;
} {
  const counts = { sharedSubject: 0, sameTeam: 0, sharedOpponent: 0 };
  for (let i = 0; i < legs.length; i += 1) {
    for (let j = i + 1; j < legs.length; j += 1) {
      const left = legs[i];
      const right = legs[j];
      if (
        left.subjectName &&
        right.subjectName &&
        left.subjectName.toLowerCase() === right.subjectName.toLowerCase()
      ) {
        counts.sharedSubject += 1;
      } else if (
        left.subjectTeam &&
        right.subjectTeam &&
        left.subjectTeam.toUpperCase() === right.subjectTeam.toUpperCase()
      ) {
        counts.sameTeam += 1;
      }
      // Shared-opponent pair detection requires event participants —
      // those aren't on TradeSelection today. The backend's authoritative
      // quote (computed at save time) DOES count shared_opponent pairs;
      // the client preview will under-credit pairs where the only
      // correlation signal is a shared opponent. Acceptable approximation
      // for v1 — the operator sees a CONSERVATIVE preview and the actual
      // saved joint can only be equal or higher.
    }
  }
  return counts;
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
  return Math.round(value * 1_000_000) / 1_000_000;
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}
