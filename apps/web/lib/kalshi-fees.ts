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

/**
 * Snap a dollar price to Kalshi's 1¢ tick, clamped to the tradable
 * 1¢–99¢ band. The exchange rejects sub-cent limit prices with
 * ``invalid_price`` — and american-odds input produces them naturally
 * (+245 → 0.2899), as do implied combo prices (0.5 × 0.5 × 0.63 …).
 * The server quantizes too; doing it here keeps the dialog's math and
 * the placed order identical.
 */
export function quantizeToCentPrice(priceDollars: number): number {
  if (!Number.isFinite(priceDollars)) return priceDollars;
  const cents = Math.round(priceDollars * 100);
  return Math.max(1, Math.min(99, cents)) / 100;
}

export function estimateTakerFeeDollars(contracts: number, priceDollars: number): number {
  if (!Number.isFinite(contracts) || !Number.isFinite(priceDollars)) return 0;
  if (contracts <= 0 || priceDollars <= 0 || priceDollars >= 1) return 0;
  const raw = 0.07 * contracts * priceDollars * (1 - priceDollars);
  // Epsilon guard: 0.07×25×0.4×0.6 floats to 0.42000000000000004,
  // which would ceil to 43¢ instead of the true 42¢.
  return Math.ceil(raw * 100 - 1e-9) / 100;
}

/**
 * Largest possible taker fee for an order that can fill at or below
 * its limit. The fee curve peaks at 50¢, so a fill below a limit over
 * 50¢ can cost more in fees than a fill at the limit itself.
 */
export function worstCaseTakerFeeDollars(
  contracts: number,
  limitPriceDollars: number,
): number {
  return estimateTakerFeeDollars(contracts, Math.min(limitPriceDollars, 0.5));
}

/**
 * Compare a cent-denominated order total with the operator's dollar cap.
 * Principal and fee are mathematically whole cents, but adding their
 * binary-float representations can produce values such as
 * 0.42000000000000004. Round the total back to cents before applying the
 * strict cap comparison so an order exactly at the cap remains allowed.
 */
export function orderTotalExceedsCap(
  principalDollars: number,
  feeDollars: number,
  capDollars: number,
): boolean {
  const totalDollars = Math.round((principalDollars + feeDollars) * 100) / 100;
  return totalDollars > capDollars;
}
