"use client";

import { useEffect, useState } from "react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { keys, openPaperPosition } from "@/lib/api";
import { usePriceDisplay } from "@/lib/price-display";

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

export function TradeDialog({
  open,
  onOpenChange,
  defaults,
  description = "Open a paper trade for this pick without leaving the trade desk.",
}: TradeDialogProps) {
  const { mode, formatEditablePrice, parsePriceInput } = usePriceDisplay();
  const [ticker, setTicker] = useState("");
  // Bug #40 phase 7 — the migrated Schema<"PaperPositionCreate">
  // narrows ``side`` to ``"yes" | "no"``. Type the state to match
  // so the submit call type-checks against the generated contract.
  const [side, setSide] = useState<"yes" | "no">("yes");
  const [quantity, setQuantity] = useState("1");
  const [priceInput, setPriceInput] = useState("");
  const [parsedPrice, setParsedPrice] = useState<number | null>(null);
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const initialPrice = defaults?.price ?? null;
    setTicker(defaults?.ticker ?? "");
    setSide(defaults?.side ?? "yes");
    setQuantity("1");
    setPriceInput(formatEditablePrice(initialPrice));
    setParsedPrice(initialPrice);
    setNotes(defaults?.notes ?? "");
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
      await openPaperPosition({
        ticker: ticker.trim().toUpperCase(),
        side,
        quantity: qty,
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
        <DialogBody className="space-y-3">
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

          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">
              Notes <span className="text-muted-foreground/50">(optional)</span>
            </label>
            <Input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Reasoning for this trade..." />
          </div>

          {error && <p className="text-xs text-negative">{error}</p>}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" size="sm" onClick={handleSubmit} disabled={loading}>
            {loading ? "Submitting..." : "Open Paper Trade"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
