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

interface PaperTradeDialogDefaults {
  ticker?: string;
  side?: string;
  entryPrice?: number;
  notes?: string;
}

interface PaperTradeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaults?: PaperTradeDialogDefaults;
  description?: string;
}

export function PaperTradeDialog({
  open,
  onOpenChange,
  defaults,
  description = "Simulate a trade without real money",
}: PaperTradeDialogProps) {
  const [ticker, setTicker] = useState("");
  const [side, setSide] = useState("yes");
  const [quantity, setQuantity] = useState("1");
  const [entryPrice, setEntryPrice] = useState("");
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setTicker(defaults?.ticker ?? "");
    setSide(defaults?.side ?? "yes");
    setQuantity("1");
    setEntryPrice(defaults?.entryPrice != null ? defaults.entryPrice.toFixed(2) : "");
    setNotes(defaults?.notes ?? "");
    setError(null);
  }, [defaults, open]);

  async function handleSubmit() {
    const qty = Number.parseInt(quantity, 10);
    const price = Number.parseFloat(entryPrice);

    if (!ticker.trim()) {
      setError("Ticker is required");
      return;
    }
    if (Number.isNaN(qty) || qty < 1) {
      setError("Quantity must be at least 1");
      return;
    }
    if (Number.isNaN(price) || price <= 0 || price >= 1) {
      setError("Entry price must be between 0 and 1");
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
      setError(caughtError instanceof Error ? caughtError.message : "Failed to open paper position");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Open Paper Position</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-3">
          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">Ticker</label>
            <Input
              mono
              className="uppercase"
              placeholder="e.g. KXNBA-2024-LAL"
              value={ticker}
              onChange={(event) => setTicker(event.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-xs text-muted-foreground">Side</label>
              <Select value={side} onValueChange={setSide}>
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
              <Input
                mono
                min="1"
                type="number"
                value={quantity}
                onChange={(event) => setQuantity(event.target.value)}
              />
            </div>
          </div>
          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">
              Entry price (0-1)
            </label>
            <Input
              mono
              max="0.99"
              min="0.01"
              placeholder="0.55"
              step="0.01"
              type="number"
              value={entryPrice}
              onChange={(event) => setEntryPrice(event.target.value)}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">
              Notes <span className="text-muted-foreground/50">(optional)</span>
            </label>
            <Input
              placeholder="Reasoning for this paper trade..."
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </div>
          {error && <p className="text-xs text-negative">{error}</p>}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="positive" size="sm" onClick={handleSubmit} disabled={loading}>
            {loading ? "Opening..." : "Open Position"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
