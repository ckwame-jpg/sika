"use client";

import { useEffect, useMemo, useState } from "react";
import { mutate } from "swr";
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
import { keys, openPaperPosition } from "@/lib/api";
import { usePriceDisplay } from "@/lib/price-display";
import { cn } from "@/lib/utils";

interface TradeDialogDefaults {
  ticker?: string;
  side?: "yes" | "no";
  price?: number;
  notes?: string;
}

interface TradeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaults?: TradeDialogDefaults;
  description?: string;
}

/**
 * Quick-stake chips. Sized to match the typical paper bet amounts the
 * operator types — clicking sets the stake field without keyboard.
 */
const QUICK_STAKE_AMOUNTS = [5, 10, 20, 50, 100] as const;

/**
 * Paper-trade dialog. Dollar-amount-first UX:
 *
 *  - The operator enters a USD stake (and optionally a price override).
 *    Quantity is computed at submit time as ``stake / effectivePrice``.
 *  - The ticker and side come from the caller's defaults in the
 *    common path (market detail sheet, trade ticket). Ticker stays
 *    hidden when known; side stays hidden because every operator
 *    pick is a YES bet in practice. Cold "New Trade" from the
 *    portfolio page still surfaces the ticker field.
 *  - Projected payout (and profit %) renders live below the stake
 *    field so the operator sees the wager's shape before submitting.
 */
export function TradeDialog({
  open,
  onOpenChange,
  defaults,
  description = "Open a paper trade for this pick without leaving the trade desk.",
}: TradeDialogProps) {
  const { mode, formatEditablePrice, formatPrice, parsePriceInput } = usePriceDisplay();
  const [ticker, setTicker] = useState("");
  // Bug #40 phase 7 — PaperPositionCreate narrows ``side`` to
  // ``"yes" | "no"``. Match the type so the submit call type-checks.
  const [side, setSide] = useState<"yes" | "no">("yes");
  const [stakeInput, setStakeInput] = useState("");
  const [priceInput, setPriceInput] = useState("");
  const [parsedPrice, setParsedPrice] = useState<number | null>(null);
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Defaults reset every time the dialog opens — stale state from a
  // previous trade shouldn't leak into the next one.
  useEffect(() => {
    if (!open) return;
    const initialPrice = defaults?.price ?? null;
    setTicker(defaults?.ticker ?? "");
    setSide(defaults?.side ?? "yes");
    setStakeInput("");
    setPriceInput(formatEditablePrice(initialPrice));
    setParsedPrice(initialPrice);
    setNotes(defaults?.notes ?? "");
    setError(null);
  }, [defaults, formatEditablePrice, open]);

  // When the operator flips the price-display mode (american / prob /
  // cents) while the dialog is open, re-render the price input string
  // from the canonical parsed value.
  useEffect(() => {
    if (!open) return;
    setPriceInput(formatEditablePrice(parsedPrice));
  }, [formatEditablePrice, mode, open, parsedPrice]);

  const hasTickerDefault = Boolean(defaults?.ticker);

  // Compute projection. We charge the side-appropriate price: YES at
  // ``p`` costs $p per $1 payout, NO at ``p`` costs $(1-p) per $1
  // payout. Bail out early on invalid inputs so the form shows "—".
  const projection = useMemo(() => {
    const stake = parseStake(stakeInput);
    if (stake == null || parsedPrice == null || parsedPrice <= 0 || parsedPrice >= 1) {
      return null;
    }
    const effective = side === "yes" ? parsedPrice : 1 - parsedPrice;
    const payout = stake / effective;
    const profit = payout - stake;
    const profitPct = (profit / stake) * 100;
    return { stake, payout, profit, profitPct };
  }, [parsedPrice, side, stakeInput]);

  async function handleSubmit() {
    const stake = parseStake(stakeInput);
    const price = parsePriceInput(priceInput);

    if (!ticker.trim()) {
      setError("Ticker is required");
      return;
    }
    if (stake == null) {
      setError("Enter a dollar amount to wager.");
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

    // Compute contract quantity from the dollar amount. YES at $0.55
    // and $20 stake → 36.36 contracts → round to 36 (paper trade, so
    // the rounding is cosmetic; the position table still tracks
    // ``quantity`` × ``entry_price`` for PnL).
    const effective = side === "yes" ? price : 1 - price;
    const computedQuantity = Math.max(1, Math.round(stake / effective));

    setLoading(true);
    setError(null);
    try {
      await openPaperPosition({
        ticker: ticker.trim().toUpperCase(),
        side,
        quantity: computedQuantity,
        entry_price: price,
        notes: notes.trim() || undefined,
      });
      await mutate(keys.positions);
      onOpenChange(false);
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Failed to submit trade");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Trade</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          {hasTickerDefault ? (
            <div className="rounded-md border border-border/60 bg-surface-hover/30 px-3 py-2">
              <p className="text-2xs uppercase tracking-wide text-muted-foreground/70">Market</p>
              <p className="mt-0.5 truncate font-mono text-xs text-foreground">{ticker}</p>
            </div>
          ) : (
            <div>
              <label className="mb-1.5 block text-xs text-muted-foreground">Ticker</label>
              <Input
                mono
                className="uppercase"
                placeholder="e.g. KXNBAGAME-2026-LAL"
                value={ticker}
                onChange={(event) => setTicker(event.target.value)}
                data-testid="trade-dialog-ticker"
              />
            </div>
          )}

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
                placeholder="20"
                value={stakeInput}
                onChange={(event) => setStakeInput(event.target.value)}
                data-testid="trade-dialog-stake"
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
                  data-testid={`trade-dialog-quick-stake-${amount}`}
                >
                  ${amount}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">
              {mode === "american" ? "Price (American odds)" : mode === "prediction" ? "Price (prediction %)" : "Price (Kalshi cents)"}
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
              data-testid="trade-dialog-price"
            />
          </div>

          <div
            className="rounded-md border border-border/40 bg-surface-hover/20 px-3 py-2.5"
            data-testid="trade-dialog-projection"
          >
            <p className="text-2xs uppercase tracking-wide text-muted-foreground/70">
              Projected payout
            </p>
            {projection ? (
              <div className="mt-1 flex items-baseline justify-between gap-3">
                <span className="font-mono text-base font-medium text-foreground">
                  ${projection.payout.toFixed(2)}
                </span>
                <span
                  className={cn(
                    "font-mono text-xs",
                    projection.profit >= 0 ? "text-positive" : "text-negative",
                  )}
                >
                  {projection.profit >= 0 ? "+" : ""}${projection.profit.toFixed(2)}
                  <span className="ml-1.5 text-muted-foreground/80">
                    ({projection.profit >= 0 ? "+" : ""}
                    {projection.profitPct.toFixed(1)}%)
                  </span>
                </span>
              </div>
            ) : (
              <p className="mt-1 font-mono text-base text-muted-foreground/60">—</p>
            )}
            {projection && parsedPrice != null && (
              <p className="mt-1 text-2xs text-muted-foreground/70">
                {side.toUpperCase()} @ {formatPrice(parsedPrice)} · if {side === "yes" ? "yes" : "no"} hits
              </p>
            )}
          </div>

          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">
              Notes <span className="text-muted-foreground/50">(optional)</span>
            </label>
            <Input
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder="Reasoning for this trade..."
            />
          </div>

          {error && <p className="text-xs text-negative">{error}</p>}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={handleSubmit}
            disabled={loading}
            data-testid="trade-dialog-submit"
          >
            {loading ? "Submitting..." : "Open Paper Trade"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Parse a stake string ("$20", "20.50", "1,000") to a positive number. */
function parseStake(raw: string): number | null {
  if (!raw.trim()) return null;
  const value = Number(raw.replace(/[$,\s]/g, ""));
  if (!Number.isFinite(value) || value <= 0) return null;
  return value;
}
