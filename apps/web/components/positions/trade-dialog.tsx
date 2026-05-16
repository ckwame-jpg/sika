"use client";

import { useEffect, useState } from "react";
import { mutate } from "swr";
import { AlertTriangle } from "lucide-react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { keys, openPaperPosition, submitDemoOrder } from "@/lib/api";
import { usePriceDisplay } from "@/lib/price-display";

interface TradeDialogDefaults {
  destination?: "paper" | "demo";
  ticker?: string;
  side?: "yes" | "no";
  action?: "buy" | "sell";
  price?: number;
  notes?: string;
}

interface TradeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaults?: TradeDialogDefaults;
  description?: string;
}

export function TradeDialog({
  open,
  onOpenChange,
  defaults,
  description = "Choose whether to route this single-market trade to paper or demo.",
}: TradeDialogProps) {
  const { mode, formatEditablePrice, parsePriceInput } = usePriceDisplay();
  const [destination, setDestination] = useState<"paper" | "demo">("paper");
  const [ticker, setTicker] = useState("");
  // Bug #40 phase 7 — the migrated Schema<"PaperPositionCreate"> /
  // Schema<"DemoOrderCreate"> types narrow ``side`` to ``"yes" | "no"``
  // and ``action`` to ``"buy" | "sell"``. Type the state to match so the
  // submit call type-checks against the generated contract.
  const [side, setSide] = useState<"yes" | "no">("yes");
  const [action, setAction] = useState<"buy" | "sell">("buy");
  const [quantity, setQuantity] = useState("1");
  const [priceInput, setPriceInput] = useState("");
  const [parsedPrice, setParsedPrice] = useState<number | null>(null);
  const [notes, setNotes] = useState("");
  const [approved, setApproved] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const initialPrice = defaults?.price ?? null;
    setDestination(defaults?.destination ?? "paper");
    setTicker(defaults?.ticker ?? "");
    setSide(defaults?.side ?? "yes");
    setAction(defaults?.action ?? "buy");
    setQuantity("1");
    setPriceInput(formatEditablePrice(initialPrice));
    setParsedPrice(initialPrice);
    setNotes(defaults?.notes ?? "");
    setApproved(false);
    setError(null);
  }, [defaults, formatEditablePrice, open]);

  useEffect(() => {
    if (!open) return;
    setPriceInput(formatEditablePrice(parsedPrice));
  }, [formatEditablePrice, mode, open, parsedPrice]);

  async function handleSubmit() {
    const qty = Number.parseInt(quantity, 10);
    const price = parsePriceInput(priceInput);

    if (!ticker.trim()) {
      setError("Ticker is required");
      return;
    }
    if (Number.isNaN(qty) || qty < 1) {
      setError("Quantity must be at least 1");
      return;
    }
    if (price == null || price <= 0 || price >= 1) {
      setError(`Enter a valid ${mode === "american" ? "American odds" : mode === "prediction" ? "probability" : "Kalshi cents"} price.`);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      if (destination === "paper") {
        await openPaperPosition({
          ticker: ticker.trim().toUpperCase(),
          side,
          quantity: qty,
          entry_price: price,
          notes: notes.trim() || undefined,
        });
      } else {
        await submitDemoOrder({
          ticker: ticker.trim().toUpperCase(),
          side,
          action,
          quantity: qty,
          limit_price: price,
          approved,
          // Bug #40 phase 7 — DemoOrderCreate's generated schema requires
          // ``time_in_force`` because its Pydantic default
          // ("good_till_canceled") is emitted as a non-optional field with
          // a default value. Supplying it explicitly here matches the
          // server-side default and keeps the call type-checked.
          time_in_force: "good_till_canceled",
        });
      }
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
        <DialogBody className="space-y-3">
          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">Destination</label>
            <Select value={destination} onValueChange={(value: "paper" | "demo") => setDestination(value)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="paper">Paper</SelectItem>
                <SelectItem value="demo">Demo</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {destination === "demo" && (
            <div className="flex items-start gap-2 rounded border border-warning/30 bg-warning/8 px-3 py-2.5 text-xs text-warning">
              <AlertTriangle size={13} className="mt-0.5 shrink-0" />
              <span>Demo orders hit the Kalshi demo API. Approval is tracked per order.</span>
            </div>
          )}

          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">Ticker</label>
            <Input
              mono
              className="uppercase"
              placeholder="e.g. KXNBAGAME-2026-LAL"
              value={ticker}
              onChange={(event) => setTicker(event.target.value)}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-xs text-muted-foreground">Side</label>
              <Select value={side} onValueChange={(value) => setSide(value as "yes" | "no")}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="yes">YES</SelectItem>
                  <SelectItem value="no">NO</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className="mb-1.5 block text-xs text-muted-foreground">Quantity</label>
              <Input mono min="1" type="number" value={quantity} onChange={(event) => setQuantity(event.target.value)} />
            </div>
          </div>

          {destination === "demo" && (
            <div>
              <label className="mb-1.5 block text-xs text-muted-foreground">Action</label>
              <Select value={action} onValueChange={(value) => setAction(value as "buy" | "sell")}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="buy">Buy</SelectItem>
                  <SelectItem value="sell">Sell</SelectItem>
                </SelectContent>
              </Select>
            </div>
          )}

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
            />
          </div>

          {destination === "paper" && (
            <div>
              <label className="mb-1.5 block text-xs text-muted-foreground">
                Notes <span className="text-muted-foreground/50">(optional)</span>
              </label>
              <Input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Reasoning for this trade..." />
            </div>
          )}

          {destination === "demo" && (
            <label className="flex cursor-pointer items-center gap-2.5">
              <input
                type="checkbox"
                checked={approved}
                onChange={(event) => setApproved(event.target.checked)}
                className="rounded border-border accent-accent"
              />
              <span className="text-xs text-foreground">Approve this demo order for submission</span>
            </label>
          )}

          {error && <p className="text-xs text-negative">{error}</p>}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" size="sm" onClick={handleSubmit} disabled={loading}>
            {loading ? "Submitting..." : destination === "paper" ? "Open Paper Trade" : approved ? "Submit Demo Order" : "Queue Demo Order"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
