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
import { estimateTakerFeeDollars } from "@/lib/kalshi-fees";
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

  const suggestedPrice = preview?.quote_yes_ask ?? preview?.implied_price ?? null;
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
                "rounded-full border px-2 py-0.5 font-mono text-2xs uppercase tracking-wide",
                environment === "live"
                  ? "border-warning/50 bg-warning/10 text-warning"
                  : "border-border/60 bg-surface-hover/40 text-muted-foreground",
              )}
              data-testid="kalshi-combo-env-badge"
            >
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
            <div className="rounded-md border border-border/60 bg-surface-hover/30 px-3 py-2">
              <p className="text-2xs uppercase tracking-wide text-muted-foreground/70">
                {legs.length} legs
              </p>
              <ul className="mt-1 space-y-0.5" data-testid="kalshi-combo-legs">
                {legs.map((leg) => (
                  <li key={leg.ticker} className="truncate text-xs text-foreground">
                    <span className="font-mono text-2xs text-muted-foreground">
                      {leg.selectedSide.toUpperCase()}
                    </span>{" "}
                    {leg.displayLabel}
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
                  if (parsed != null) setParsedPrice(parsed);
                }}
                data-testid="kalshi-combo-price"
              />
            </div>

            <div className="rounded-md border border-border/40 bg-surface-hover/20 px-3 py-2.5">
              <p className="text-2xs uppercase tracking-wide text-muted-foreground/70">Order</p>
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
              className="rounded-md border border-warning/40 bg-warning/5 px-3 py-3"
              data-testid="kalshi-combo-confirm-summary"
            >
              <p className="text-2xs uppercase tracking-wide text-muted-foreground/70">
                Confirm {environment === "live" ? "real" : "sandbox"} combo ·{" "}
                {legs.length} legs
              </p>
              <p className="mt-0.5 font-mono text-xs text-muted-foreground">
                LIMIT YES @ {parsedPrice != null ? formatPrice(parsedPrice) : "—"} on the
                combo market
              </p>
              {order && (
                <dl className="mt-2 space-y-1 font-mono text-xs">
                  <div className="flex justify-between">
                    <dt className="text-muted-foreground">Contracts</dt>
                    <dd className="text-foreground">{order.quantity}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-muted-foreground">Total cost</dt>
                    <dd className="text-foreground">${order.cost.toFixed(2)}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-muted-foreground">Est. fee (taker)</dt>
                    <dd className="text-foreground">${order.fee.toFixed(2)}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-muted-foreground">Pays if ALL legs hit</dt>
                    <dd className="text-positive">${order.payout.toFixed(2)}</dd>
                  </div>
                  {capDollars != null && (
                    <div className="flex justify-between">
                      <dt className="text-muted-foreground">Per-order cap</dt>
                      <dd className="text-muted-foreground">${capDollars.toFixed(0)}</dd>
                    </div>
                  )}
                </dl>
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
              <Button
                variant="primary"
                size="sm"
                onClick={handleReview}
                disabled={!order || overCap || legs.length < 2}
                data-testid="kalshi-combo-review"
              >
                Review combo
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" size="sm" onClick={() => setStage("form")}>
                Back
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={handleConfirm}
                disabled={loading || !order || overCap}
                data-testid="kalshi-combo-confirm"
              >
                {loading
                  ? "Placing..."
                  : environment === "live"
                    ? "Confirm — place real combo"
                    : "Confirm — place sandbox combo"}
              </Button>
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
