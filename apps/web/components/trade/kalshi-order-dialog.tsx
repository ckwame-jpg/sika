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
import { fetchTradingSettings, keys, placeKalshiOrder } from "@/lib/api";
import { estimateTakerFeeDollars } from "@/lib/kalshi-fees";
import { usePriceDisplay } from "@/lib/price-display";
import { cn } from "@/lib/utils";

interface KalshiOrderDialogDefaults {
  ticker: string;
  side: "yes" | "no";
  price?: number;
  displayLabel?: string;
  eventName?: string;
  /** Model win probability for the selected side (0–1) — drives the
   * arrival gauge so the dialog states WHAT you're arming first. */
  probability?: number | null;
}

interface KalshiOrderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaults: KalshiOrderDialogDefaults;
  /** "live" | "demo" — from the user's stored credentials base_url. */
  environment: "live" | "demo";
}

const QUICK_STAKE_AMOUNTS = [5, 10, 20, 50, 100] as const;

/**
 * Real-order dialog — the live-money sibling of ``TradeDialog``.
 *
 * Same dollar-first UX (stake + limit price → contracts), but with a
 * mandatory two-stage flow: the form computes the order, the CONFIRM
 * stage restates exactly what will hit the exchange (side, limit
 * price, contracts, total cost, estimated taker fee, payout, the
 * operator's per-order cap) and only then allows "place real order".
 * The order itself is a LIMIT at the shown price — it can rest; the
 * portfolio orders panel is where resting orders get cancelled.
 */
export function KalshiOrderDialog({
  open,
  onOpenChange,
  defaults,
  environment,
}: KalshiOrderDialogProps) {
  const { mode, formatEditablePrice, formatPrice, parsePriceInput } = usePriceDisplay();
  const [stage, setStage] = useState<"form" | "confirm">("form");
  const [stakeInput, setStakeInput] = useState("");
  const [priceInput, setPriceInput] = useState("");
  const [parsedPrice, setParsedPrice] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: tradingSettings } = useSWR(keys.tradingSettings, fetchTradingSettings);
  const capDollars = tradingSettings?.max_order_cost_dollars ?? null;

  // Reset on the OPEN TRANSITION only — ``defaults`` is a fresh object
  // literal every parent render; depending on it would wipe in-progress
  // input on the trade desk's 30s poll (same footgun as TradeDialog).
  useEffect(() => {
    if (!open) return;
    const initialPrice = defaults.price ?? null;
    setStage("form");
    setStakeInput("");
    setPriceInput(formatEditablePrice(initialPrice));
    setParsedPrice(initialPrice);
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
    const payout = quantity; // $1 per contract if the side hits
    return { stake, quantity, cost, fee, payout, profit: payout - cost - fee };
  }, [parsedPrice, stakeInput]);

  const overCap = order != null && capDollars != null && order.cost > capDollars;

  function handleReview() {
    const price = parsePriceInput(priceInput);
    if (parseStake(stakeInput) == null) {
      setError("Enter a dollar amount.");
      return;
    }
    if (price == null || price <= 0 || price >= 1) {
      setError(
        `Enter a valid ${
          mode === "american" ? "American odds" : mode === "prediction" ? "probability" : "Kalshi cents"
        } price.`,
      );
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
      await placeKalshiOrder({
        ticker: defaults.ticker.toUpperCase(),
        side: defaults.side,
        action: "buy",
        quantity: order.quantity,
        limit_price: parsedPrice,
        approved: true,
        time_in_force: "good_till_canceled",
      });
      await Promise.all([mutate(keys.kalshiOrders), mutate(keys.positions)]);
      onOpenChange(false);
    } catch (caughtError) {
      setError(
        caughtError instanceof Error ? caughtError.message : "Failed to place order",
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
            Place on Kalshi
            <span
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-2xs uppercase tracking-wide",
                environment === "live"
                  ? "border-warning/50 bg-warning/10 text-warning"
                  : "border-border/60 bg-surface-hover/40 text-muted-foreground",
              )}
              data-testid="kalshi-order-env-badge"
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
            Limit order at your price — it never fills worse than shown, but may rest
            until the market meets it.
          </DialogDescription>
        </DialogHeader>

        {stage === "form" ? (
          <DialogBody className="space-y-4">
            {/* Arrival strip — the pick's identity before any money input:
                win-prob gauge + label + side/price chips (spec gauge-card
                layout at dialog scale). */}
            <div className="gi-card flex items-center gap-3.5" data-testid="kalshi-order-arrival">
              {defaults.probability != null && (
                <div
                  className="gi-gauge sm"
                  style={
                    {
                      "--gg-p": Math.max(0, Math.min(100, defaults.probability * 100)),
                      "--gg-c": "var(--color-cosmos-cyan-500)",
                    } as React.CSSProperties
                  }
                  aria-hidden
                >
                  <span className="gi-gauge-value">
                    {Math.round(defaults.probability * 100)}%
                  </span>
                </div>
              )}
              <div className="min-w-0 flex-1">
                <p className="gi-micro-label">
                  {defaults.probability != null ? "win probability · " : ""}
                  {defaults.eventName ?? "market"}
                </p>
                <p className="mt-0.5 truncate text-sm font-medium text-foreground">
                  {defaults.displayLabel ?? defaults.ticker}
                </p>
                <p className="truncate font-mono text-2xs text-muted-foreground">
                  {defaults.side.toUpperCase()}
                  {defaults.price != null && <> @ {formatPrice(defaults.price)}</>}
                  {" · "}
                  {defaults.ticker}
                </p>
              </div>
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
                  data-testid="kalshi-order-stake"
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
                {mode === "american"
                  ? "Limit price (American odds)"
                  : mode === "prediction"
                    ? "Limit price (prediction %)"
                    : "Limit price (Kalshi cents)"}
              </label>
              <Input
                mono
                placeholder={mode === "american" ? "-110" : mode === "prediction" ? "54.0" : "55"}
                value={priceInput}
                onChange={(event) => {
                  const value = event.target.value;
                  setPriceInput(value);
                  const parsed = parsePriceInput(value);
                  if (parsed != null) setParsedPrice(parsed);
                }}
                data-testid="kalshi-order-price"
              />
            </div>

            <div className="gi-card !py-2.5">
              <p className="gi-micro-label">Order</p>
              {order ? (
                <p className="mt-1 font-mono text-sm text-foreground" data-testid="kalshi-order-preview">
                  {order.quantity} contract{order.quantity === 1 ? "" : "s"} · cost $
                  {order.cost.toFixed(2)} · pays ${order.payout.toFixed(2)} if{" "}
                  {defaults.side === "yes" ? "yes" : "no"}
                </p>
              ) : (
                <p className="mt-1 font-mono text-sm text-muted-foreground/60">—</p>
              )}
              {overCap && capDollars != null && (
                <p className="mt-1 text-2xs text-negative" data-testid="kalshi-order-cap-warning">
                  Over your ${capDollars.toFixed(0)} per-order cap — lower the stake or raise
                  the cap in settings.
                </p>
              )}
            </div>

            {error && <p className="text-xs text-negative">{error}</p>}
          </DialogBody>
        ) : (
          <DialogBody className="space-y-3">
            <div
              className="gi-armed-card px-4 py-3.5"
              data-testid="kalshi-order-confirm-summary"
            >
              <p className="gi-micro-label">
                Confirm {environment === "live" ? "real" : "sandbox"} order
              </p>
              <p className="mt-1.5 truncate text-sm font-medium text-foreground">
                {defaults.displayLabel ?? defaults.ticker}
              </p>
              <p className="mt-0.5 font-mono text-xs text-muted-foreground">
                LIMIT {defaults.side.toUpperCase()} @ {parsedPrice != null ? formatPrice(parsedPrice) : "—"}
              </p>
              {order && (
                <>
                  {/* The one edited sentence — the wager in human terms. */}
                  <p className="mt-2.5 text-[13px] leading-snug text-foreground/90" data-testid="kalshi-order-human-line">
                    risking{" "}
                    <span className="font-mono text-foreground">${order.cost.toFixed(2)}</span> for a
                    shot at{" "}
                    <span className="font-mono text-positive">${order.payout.toFixed(2)}</span> if it
                    hits — fee ~
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
                    <div className="gi-stat-chip">
                      <span className="k">Pays if it hits</span>
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
              Limit orders can rest until filled — cancel anytime from the portfolio
              orders panel. Resting fills are charged the lower maker fee.
            </p>
            {error && <p className="text-xs text-negative" role="alert">{error}</p>}
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
                disabled={!order || overCap}
                data-testid="kalshi-order-review"
              >
                Review order
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
                data-testid="kalshi-order-confirm"
              >
                {environment === "live" && !loading && <span className="dot" aria-hidden />}
                {loading
                  ? "Placing..."
                  : environment === "live"
                    ? "Confirm — place real order"
                    : "Confirm — place sandbox order"}
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
