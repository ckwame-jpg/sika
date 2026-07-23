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
import {
  estimateTakerFeeDollars,
  orderTotalExceedsCap,
  quantizeToCentPrice,
  worstCaseTakerFeeDollars,
} from "@/lib/kalshi-fees";
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

/** Fill-now price buffer: bid this many cents above the reference
 * price so a thin/moving book can't dodge the order between glance
 * and tap. A limit is the WORST acceptable price — the exchange still
 * fills at the actual ask, so the buffer costs nothing when the book
 * holds still. */
const FILL_NOW_BUFFER = 0.03;

type FillMode = "fill_now" | "rest";

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
  const [fillMode, setFillMode] = useState<FillMode>("fill_now");
  const [stakeInput, setStakeInput] = useState("");
  const [priceInput, setPriceInput] = useState("");
  const [parsedPrice, setParsedPrice] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reference price (the market's side price) and the per-mode prefill:
  // fill-now bids reference + buffer so the book can't dodge; rest
  // bids the reference itself and waits.
  const referencePrice = defaults.price != null ? quantizeToCentPrice(defaults.price) : null;
  const prefillFor = (nextMode: FillMode) =>
    referencePrice == null
      ? null
      : nextMode === "fill_now"
        ? quantizeToCentPrice(referencePrice + FILL_NOW_BUFFER)
        : referencePrice;

  function switchFillMode(nextMode: FillMode) {
    setFillMode(nextMode);
    const price = prefillFor(nextMode);
    setParsedPrice(price);
    setPriceInput(formatEditablePrice(price));
  }

  const { data: tradingSettings } = useSWR(keys.tradingSettings, fetchTradingSettings);
  const capDollars = tradingSettings?.max_order_cost_dollars ?? null;

  // Reset on the OPEN TRANSITION only — ``defaults`` is a fresh object
  // literal every parent render; depending on it would wipe in-progress
  // input on the trade desk's 30s poll (same footgun as TradeDialog).
  useEffect(() => {
    if (!open) return;
    // Default to fill-now: "place the bet" semantics. The buffered
    // prefill is the worst-case price; actual fills happen at the ask.
    const initialPrice =
      referencePrice != null
        ? quantizeToCentPrice(referencePrice + FILL_NOW_BUFFER)
        : null;
    setStage("form");
    setFillMode("fill_now");
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
    const worstCaseFee = worstCaseTakerFeeDollars(quantity, parsedPrice);
    const payout = quantity; // $1 per contract if the side hits
    return { stake, quantity, cost, fee, worstCaseFee, payout, profit: payout - cost - fee };
  }, [parsedPrice, stakeInput]);

  const overCap =
    order != null &&
    capDollars != null &&
    orderTotalExceedsCap(order.cost, order.worstCaseFee, capDollars);

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
        // Fill-now = IOC: execute what the book offers up to the max
        // price, cancel the rest — never leaves a zombie resting order.
        time_in_force: fillMode === "fill_now" ? "immediate_or_cancel" : "good_till_canceled",
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

            <div className="flex gap-1.5" role="radiogroup" aria-label="Order mode">
              <button
                type="button"
                role="radio"
                aria-checked={fillMode === "fill_now"}
                onClick={() => switchFillMode("fill_now")}
                className={cn(
                  "flex-1 rounded-lg border px-3 py-2 text-left transition-colors duration-[120ms] focus-visible:ring-focus",
                  fillMode === "fill_now"
                    ? "border-accent/60 bg-accent/10"
                    : "border-border/60 hover:border-accent/40",
                )}
                data-testid="kalshi-order-mode-fill"
              >
                <span className="block text-xs font-medium text-foreground">fill now</span>
                <span className="block text-2xs text-muted-foreground">
                  takes the market price · instant or nothing
                </span>
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={fillMode === "rest"}
                onClick={() => switchFillMode("rest")}
                className={cn(
                  "flex-1 rounded-lg border px-3 py-2 text-left transition-colors duration-[120ms] focus-visible:ring-focus",
                  fillMode === "rest"
                    ? "border-accent/60 bg-accent/10"
                    : "border-border/60 hover:border-accent/40",
                )}
                data-testid="kalshi-order-mode-rest"
              >
                <span className="block text-xs font-medium text-foreground">rest at my price</span>
                <span className="block text-2xs text-muted-foreground">
                  waits on the book · better odds, may never fill
                </span>
              </button>
            </div>

            <div>
              <label className="mb-1.5 block text-xs text-muted-foreground">
                {fillMode === "fill_now" ? "Max price" : "Limit price"}
                {mode === "american"
                  ? " (American odds)"
                  : mode === "prediction"
                    ? " (prediction %)"
                    : " (Kalshi cents)"}
                {fillMode === "fill_now" && (
                  <span className="text-muted-foreground/60">
                    {" "}
                    · fills at the best available up to this
                  </span>
                )}
              </label>
              <Input
                mono
                placeholder={mode === "american" ? "-110" : mode === "prediction" ? "54.0" : "55"}
                value={priceInput}
                onChange={(event) => {
                  const value = event.target.value;
                  setPriceInput(value);
                  const parsed = parsePriceInput(value);
                  // american odds → probability produces sub-cent
                  // values; snap so the math matches what Kalshi
                  // will actually accept.
                  if (parsed != null) setParsedPrice(quantizeToCentPrice(parsed));
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
                  Principal plus worst-case taker fee is over your ${capDollars.toFixed(0)}
                  {" "}per-order cap — lower the stake or raise the cap in settings.
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
                {fillMode === "fill_now" ? "FILL NOW" : "REST"} · {defaults.side.toUpperCase()} up to{" "}
                {parsedPrice != null ? formatPrice(parsedPrice) : "—"}
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
              {fillMode === "fill_now"
                ? "Executes instantly at the best available price up to your max — cost and fee shown are the worst case. If the book is empty, nothing happens and nothing is charged."
                : "Rests on the book until someone meets your price — cancel anytime from the portfolio orders panel. Resting fills are charged the lower maker fee."}
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
