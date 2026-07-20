"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR, { mutate } from "swr";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { fetchTradingSettings, keys, placeKalshiCombo } from "@/lib/api";
import { estimateTakerFeeDollars, quantizeToCentPrice } from "@/lib/kalshi-fees";
import type { KalshiComboLegCreate, KalshiComboPreviewRead } from "@/lib/types";
import { usePriceDisplay } from "@/lib/price-display";
import { cn } from "@/lib/utils";
import { useParlayTray } from "./parlay-tray-store";

interface KalshiComboDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  environment: "live" | "demo";
  /** Latest tray preview — supplies collection/quote/implied price. */
  preview: KalshiComboPreviewRead | null;
}

const QUICK_STAKE_AMOUNTS = [5, 10, 20, 50] as const;

/**
 * Real combo (parlay) order dialog — the tray's live-money exit.
 *
 * Reads legs from the tray store; the limit price prefills from the
 * live combo quote (yes ask) when the combo market already exists,
 * else from the multiplicative implied price. Same form → confirm
 * two-stage flow as ``KalshiOrderDialog``; on success the tray clears
 * (those legs are now a real order, not a draft).
 */
export function KalshiComboDialog({
  open,
  onOpenChange,
  environment,
  preview,
}: KalshiComboDialogProps) {
  const { mode, formatEditablePrice, formatPrice, parsePriceInput } = usePriceDisplay();
  const { legs, clear } = useParlayTray();
  const [stage, setStage] = useState<"form" | "confirm">("form");
  const [stakeInput, setStakeInput] = useState("");
  const [priceInput, setPriceInput] = useState("");
  const [parsedPrice, setParsedPrice] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: tradingSettings } = useSWR(keys.tradingSettings, fetchTradingSettings);
  const capDollars = tradingSettings?.max_order_cost_dollars ?? null;

  // Cent-snap: implied multiplicative prices are essentially never
  // cent-aligned, and Kalshi rejects sub-cent limits (invalid_price).
  const rawSuggested = preview?.quote_yes_ask ?? preview?.implied_price ?? null;
  const suggestedPrice = rawSuggested != null ? quantizeToCentPrice(rawSuggested) : null;
  const mintNeeded = !preview?.existing_market_ticker;

  // Reset on open transition only (same footgun note as TradeDialog).
  useEffect(() => {
    if (!open) return;
    setStage("form");
    setStakeInput("");
    setPriceInput(formatEditablePrice(suggestedPrice));
    setParsedPrice(suggestedPrice);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setPriceInput(formatEditablePrice(parsedPrice));
  }, [formatEditablePrice, mode, open, parsedPrice]);

  const order = useMemo(() => {
    const stake = parseStake(stakeInput);
    if (stake == null || parsedPrice == null || parsedPrice <= 0 || parsedPrice >= 1) {
      return null;
    }
    const quantity = Math.max(1, Math.round(stake / parsedPrice));
    const cost = quantity * parsedPrice;
    const fee = estimateTakerFeeDollars(quantity, parsedPrice);
    return { stake, quantity, cost, fee, payout: quantity };
  }, [parsedPrice, stakeInput]);

  const overCap = order != null && capDollars != null && order.cost > capDollars;

  function legPayload(): KalshiComboLegCreate[] {
    return legs.map((leg) => ({
      ticker: leg.ticker,
      side: leg.selectedSide.toLowerCase() as "yes" | "no",
      entry_price: leg.entryPrice ?? null,
      market_title: leg.displayLabel ?? leg.marketTitle ?? null,
      subject_name: leg.subjectName ?? null,
      stat_key: leg.statKey ?? null,
      threshold: leg.threshold ?? null,
    }));
  }

  function handleReview() {
    const price = parsePriceInput(priceInput);
    if (parseStake(stakeInput) == null) {
      setError("Enter a dollar amount.");
      return;
    }
    if (price == null || price <= 0 || price >= 1) {
      setError("Enter a valid combo price.");
      return;
    }
    setError(null);
    setStage("confirm");
  }

  async function handleConfirm() {
    if (!order || parsedPrice == null) return;
    setLoading(true);
    setError(null);
    try {
      await placeKalshiCombo({
        legs: legPayload(),
        quantity: order.quantity,
        limit_price: parsedPrice,
        approved: true,
        time_in_force: "good_till_canceled",
      });
      await Promise.all([mutate(keys.kalshiOrders), mutate(keys.positions)]);
      clear();
      onOpenChange(false);
    } catch (caughtError) {
      setError(
        caughtError instanceof Error ? caughtError.message : "Failed to place combo",
      );
      setStage("form");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            Place combo on Kalshi
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-2xs uppercase tracking-wide",
                environment === "live"
                  ? "border-warning/50 bg-warning/10 text-warning"
                  : "border-border/60 bg-surface-hover/40 text-muted-foreground",
              )}
              data-testid="kalshi-combo-env-badge"
            >
              <span
                className={cn(
                  "h-[5px] w-[5px] rounded-full",
                  environment === "live"
                    ? "bg-warning shadow-[0_0_6px_rgba(247,141,108,0.8)]"
                    : "bg-muted-foreground/60",
                )}
                aria-hidden
              />
              {environment === "live" ? "live · real money" : "demo / sandbox"}
            </span>
          </DialogTitle>
          <DialogDescription>
            A real combo market — every leg must hit to pay out. Limit order at your
            price; it may rest until the book meets it.
          </DialogDescription>
        </DialogHeader>

        {stage === "form" ? (
          <DialogBody className="space-y-4">
            {/* Arrival — the combo's identity as tray-style chips before
                any money input. */}
            <div className="gi-card" data-testid="kalshi-combo-legs">
              <p className="gi-micro-label">
                {legs.length}-leg combo · all must hit
              </p>
              <ul className="mt-2 space-y-1.5">
                {legs.map((leg) => (
                  <li
                    key={leg.ticker}
                    className="flex items-center gap-2 rounded-lg border border-border/40 bg-surface-hover/25 px-2.5 py-1.5"
                  >
                    <span className="h-[5px] w-[5px] shrink-0 rounded-full bg-accent/70" aria-hidden />
                    <span className="min-w-0 flex-1 truncate text-xs text-foreground">
                      {leg.displayLabel}
                    </span>
                    <span className="shrink-0 font-mono text-2xs text-muted-foreground">
                      {leg.selectedSide.toUpperCase()}
                      {leg.entryPrice != null && ` ${(leg.entryPrice * 100).toFixed(0)}¢`}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            <div>
              <label className="mb-1.5 block text-xs text-muted-foreground">
                How much you&apos;re putting in
              </label>
              <div className="relative">
                <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-xs text-muted-foreground">
                  $
                </span>
                <Input
                  mono
                  className="pl-6"
                  inputMode="decimal"
                  placeholder="10"
                  value={stakeInput}
                  onChange={(event) => setStakeInput(event.target.value)}
                  data-testid="kalshi-combo-stake"
                  autoFocus
                />
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {QUICK_STAKE_AMOUNTS.map((amount) => (
                  <button
                    key={amount}
                    type="button"
                    onClick={() => setStakeInput(String(amount))}
                    className={cn(
                      "rounded-full border border-border/60 px-2.5 py-0.5 font-mono text-2xs text-muted-foreground transition-colors duration-[120ms]",
                      "hover:border-accent/40 hover:text-foreground focus-visible:ring-focus",
                      parseStake(stakeInput) === amount && "border-accent/60 bg-accent/10 text-accent",
                    )}
                  >
                    ${amount}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="mb-1.5 block text-xs text-muted-foreground">
                Combo limit price
                {preview?.quote_yes_ask != null
                  ? " (live ask)"
                  : preview?.implied_price != null
                    ? " (implied from legs)"
                    : ""}
              </label>
              <Input
                mono
                value={priceInput}
                onChange={(event) => {
                  const value = event.target.value;
                  setPriceInput(value);
                  const parsed = parsePriceInput(value);
                  if (parsed != null) setParsedPrice(quantizeToCentPrice(parsed));
                }}
                data-testid="kalshi-combo-price"
              />
            </div>

            <div className="gi-card !py-2.5">
              <p className="gi-micro-label">Order</p>
              {order ? (
                <p className="mt-1 font-mono text-sm text-foreground" data-testid="kalshi-combo-preview-line">
                  {order.quantity} contract{order.quantity === 1 ? "" : "s"} · cost $
                  {order.cost.toFixed(2)} · pays ${order.payout.toFixed(2)} if all legs hit
                </p>
              ) : (
                <p className="mt-1 font-mono text-sm text-muted-foreground/60">—</p>
              )}
              {overCap && capDollars != null && (
                <p className="mt-1 text-2xs text-negative" data-testid="kalshi-combo-cap-warning">
                  Over your ${capDollars.toFixed(0)} per-order cap.
                </p>
              )}
            </div>

            {error && <p className="text-xs text-negative">{error}</p>}
          </DialogBody>
        ) : (
          <DialogBody className="space-y-3">
            <div
              className="gi-armed-card px-4 py-3.5"
              data-testid="kalshi-combo-confirm-summary"
            >
              <p className="gi-micro-label">
                Confirm {environment === "live" ? "real" : "sandbox"} combo ·{" "}
                {legs.length} legs
              </p>
              <p className="mt-1 font-mono text-xs text-muted-foreground">
                LIMIT YES @ {parsedPrice != null ? formatPrice(parsedPrice) : "—"} on the
                combo market
              </p>
              {order && (
                <>
                  <p className="mt-2.5 text-[13px] leading-snug text-foreground/90" data-testid="kalshi-combo-human-line">
                    risking{" "}
                    <span className="font-mono text-foreground">${order.cost.toFixed(2)}</span> for a
                    shot at{" "}
                    <span className="font-mono text-positive">${order.payout.toFixed(2)}</span> if all{" "}
                    {legs.length} legs hit — fee ~
                    <span className="font-mono text-foreground">${order.fee.toFixed(2)}</span>.
                  </p>
                  <div className="mt-3 grid grid-cols-3 gap-2">
                    <div className="gi-stat-chip">
                      <span className="k">Contracts</span>
                      <span className="v">{order.quantity}</span>
                    </div>
                    <div className="gi-stat-chip">
                      <span className="k">Total cost</span>
                      <span className="v">${order.cost.toFixed(2)}</span>
                    </div>
                    <div className="gi-stat-chip">
                      <span className="k">Est. fee (taker)</span>
                      <span className="v">${order.fee.toFixed(2)}</span>
                    </div>
                    <div className="gi-stat-chip col-span-2">
                      <span className="k">Pays if ALL legs hit</span>
                      <span className="v pos">${order.payout.toFixed(2)}</span>
                    </div>
                    {capDollars != null && (
                      <div className="gi-stat-chip">
                        <span className="k">Per-order cap</span>
                        <span className="v">${capDollars.toFixed(0)}</span>
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
            <p className="text-2xs text-muted-foreground">
              {mintNeeded
                ? "The combo market will be created on Kalshi if it doesn't exist yet — fresh books are thin, so the order will likely rest at first. "
                : ""}
              Cancel anytime from the portfolio orders panel.
            </p>
            {error && <p className="text-xs text-negative">{error}</p>}
          </DialogBody>
        )}

        <DialogFooter>
          {stage === "form" ? (
            <>
              <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <button
                type="button"
                className="gi-btn"
                onClick={handleReview}
                disabled={!order || overCap || legs.length < 2}
                data-testid="kalshi-combo-review"
              >
                Review combo
              </button>
            </>
          ) : (
            <>
              <Button variant="ghost" size="sm" onClick={() => setStage("form")}>
                Back
              </Button>
              <button
                type="button"
                className="gi-btn-live"
                onClick={handleConfirm}
                disabled={loading || !order || overCap}
                data-testid="kalshi-combo-confirm"
              >
                {environment === "live" && !loading && <span className="dot" aria-hidden />}
                {loading
                  ? "Placing..."
                  : environment === "live"
                    ? "Confirm — place real combo"
                    : "Confirm — place sandbox combo"}
              </button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function parseStake(raw: string): number | null {
  if (!raw.trim()) return null;
  const value = Number(raw.replace(/[$,\s]/g, ""));
  if (!Number.isFinite(value) || value <= 0) return null;
  return value;
}
