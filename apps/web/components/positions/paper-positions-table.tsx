"use client";

import { useEffect, useState } from "react";
import useSWR, { mutate } from "swr";
import { fetchPositions, exitPaperPosition, keys } from "@/lib/api";
import type { PositionsRead, PaperPositionRead } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SkeletonRow } from "@/components/ui/skeleton";
import { MarketDetailSheet } from "@/components/markets/market-detail-sheet";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogBody,
  DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  fmtContractPnl,
  fmtDatetime,
  pnlClass,
  sideClass,
} from "@/lib/utils";
import { cn } from "@/lib/utils";
import { usePriceDisplay } from "@/lib/price-display";

function PnlCell({ pnl, status }: { pnl: number | null; status: string }) {
  if (status === "open") return <span className="font-mono text-xs text-muted-foreground">Open</span>;
  if (pnl == null) return <span className="font-mono text-xs text-muted-foreground">—</span>;
  return (
    <span className={cn("font-mono text-xs font-medium", pnlClass(pnl))}>
      {fmtContractPnl(pnl)}
    </span>
  );
}

function ExitDialog({
  position,
  onClose,
}: {
  position: PaperPositionRead;
  onClose: () => void;
}) {
  const { mode, formatEditablePrice, formatPrice, parsePriceInput } = usePriceDisplay();
  const [exitPrice, setExitPrice] = useState(formatEditablePrice(position.entry_price));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setExitPrice(formatEditablePrice(position.entry_price));
  }, [formatEditablePrice, mode, position.entry_price]);

  async function handleExit() {
    const price = parsePriceInput(exitPrice);
    if (price == null || price <= 0 || price >= 1) {
      setError("Enter a valid exit price.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await exitPaperPosition(position.id, { exit_price: price });
      await mutate(keys.positions);
      onClose();
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Failed to exit position");
    } finally {
      setLoading(false);
    }
  }

  const previewExitPrice = parsePriceInput(exitPrice);
  const previewPnl = previewExitPrice == null
    ? null
    : (previewExitPrice - position.entry_price) * position.quantity;

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Exit Position</DialogTitle>
          <DialogDescription>
            {position.ticker} · {position.side.toUpperCase()} · {position.quantity} contracts
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-3">
          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">
              {mode === "american" ? "Exit price (American odds)" : mode === "prediction" ? "Exit price (prediction %)" : "Exit price (Kalshi cents)"}
            </label>
            <Input
              mono
              value={exitPrice}
              onChange={(event) => setExitPrice(event.target.value)}
              placeholder={mode === "american" ? "-110" : mode === "prediction" ? "54.0" : "55"}
            />
          </div>
          {previewExitPrice != null && (
            <div className="text-xs text-muted-foreground">
              Entry: {formatPrice(position.entry_price)} →
              Exit: {formatPrice(previewExitPrice)} ·
              PnL:{" "}
              <span
                className={pnlClass(previewPnl)}
              >
                {fmtContractPnl(previewPnl)}
              </span>
            </div>
          )}
          {error && <p className="text-xs text-negative">{error}</p>}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="danger"
            size="sm"
            onClick={handleExit}
            disabled={loading}
          >
            {loading ? "Exiting..." : "Exit Position"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PositionRow({
  position,
  onViewMarket,
  onExit,
}: {
  position: PaperPositionRead;
  onViewMarket: () => void;
  onExit: () => void;
}) {
  const { formatPrice } = usePriceDisplay();
  const isOpen = position.status === "open";

  return (
    <TableRow>
      <TableCell>
        <button
          className="cursor-pointer font-mono text-xs text-accent hover:underline"
          onClick={onViewMarket}
        >
          {position.ticker}
        </button>
      </TableCell>
      <TableCell>
        <span className={cn("font-mono text-xs font-medium", sideClass(position.side))}>
          {position.side.toUpperCase()}
        </span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{position.quantity}</span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{formatPrice(position.entry_price)}</span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs text-muted-foreground">
          {formatPrice(position.exit_price)}
        </span>
      </TableCell>
      <TableCell>
        <PnlCell pnl={position.pnl} status={position.status} />
      </TableCell>
      <TableCell>
        <Badge variant={isOpen ? "positive" : "default"}>
          {position.status}
        </Badge>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs text-muted-foreground">
          {fmtDatetime(position.opened_at)}
        </span>
      </TableCell>
      <TableCell>
        {isOpen && (
          <Button variant="danger" size="xs" onClick={onExit}>
            Exit
          </Button>
        )}
      </TableCell>
    </TableRow>
  );
}

interface PaperPositionsTableProps {
  maxHeight?: string;
}

export function PaperPositionsTable({ maxHeight }: PaperPositionsTableProps) {
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [exitingPosition, setExitingPosition] = useState<PaperPositionRead | null>(null);

  const { data, isLoading, error } = useSWR<PositionsRead>(
    keys.positions,
    fetchPositions,
    { refreshInterval: 15_000 },
  );

  const positions = data?.paper_positions ?? [];
  const wrapperClassName = maxHeight ? "overflow-auto" : "overflow-x-auto";
  const wrapperStyle = maxHeight ? { maxHeight } : undefined;

  if (error) {
    return (
      <div className="flex h-24 items-center justify-center text-xs text-negative">
        Failed to load positions.
      </div>
    );
  }

  return (
    <>
      <div className={wrapperClassName} style={wrapperStyle}>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Ticker</TableHead>
              <TableHead className="w-14">Side</TableHead>
              <TableHead className="w-16">Qty</TableHead>
              <TableHead className="w-20">Entry</TableHead>
              <TableHead className="w-20">Exit</TableHead>
              <TableHead className="w-20">PnL</TableHead>
              <TableHead className="w-20">Status</TableHead>
              <TableHead className="w-36">Opened</TableHead>
              <TableHead className="w-16" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading
              ? Array.from({ length: 5 }).map((_, index) => <SkeletonRow key={index} cols={9} />)
              : positions.length === 0
                ? (
                  <TableRow>
                    <TableCell colSpan={9} className="py-8 text-center text-xs text-muted-foreground">
                      No paper positions yet
                    </TableCell>
                  </TableRow>
                )
                : positions.map((position) => (
                    <PositionRow
                      key={position.id}
                      position={position}
                      onViewMarket={() => setSelectedTicker(position.ticker)}
                      onExit={() => setExitingPosition(position)}
                    />
                  ))}
          </TableBody>
        </Table>
      </div>

      <MarketDetailSheet
        ticker={selectedTicker}
        onClose={() => setSelectedTicker(null)}
      />

      {exitingPosition && (
        <ExitDialog
          position={exitingPosition}
          onClose={() => setExitingPosition(null)}
        />
      )}
    </>
  );
}
