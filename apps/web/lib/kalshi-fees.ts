/**
 * Kalshi trading-fee estimate for the real-order confirm dialogs.
 *
 * Kalshi's published formula (fee schedule PDF): taker fee =
 * ceil_to_cent(0.07 × contracts × price × (1 − price)); resting
 * (maker) fills are charged 0.0175 in place of 0.07. We show the
 * taker estimate — the conservative (higher) number — and note that
 * resting fills cost ~¼ of it. Actual fees come back on fills and are
 * stored server-side (KalshiOrderFill.fee_dollars).
 */

export function estimateTakerFeeDollars(contracts: number, priceDollars: number): number {
  if (!Number.isFinite(contracts) || !Number.isFinite(priceDollars)) return 0;
  if (contracts <= 0 || priceDollars <= 0 || priceDollars >= 1) return 0;
  const raw = 0.07 * contracts * priceDollars * (1 - priceDollars);
  // Epsilon guard: 0.07×25×0.4×0.6 floats to 0.42000000000000004,
  // which would ceil to 43¢ instead of the true 42¢.
  return Math.ceil(raw * 100 - 1e-9) / 100;
}
