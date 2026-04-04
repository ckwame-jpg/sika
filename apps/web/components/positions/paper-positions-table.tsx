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
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
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

function PositionCard({
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
    <Card className="bg-surface-hover shadow-none">
      <CardContent className="space-y-3 px-4 py-4">
        <div className="flex items-start justify-between gap-3">
          <button
            className="min-w-0 cursor-pointer text-left"
            onClick={onViewMarket}
          >
            <p className="truncate font-mono text-xs text-accent hover:underline">
              {position.ticker}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              Opened {fmtDatetime(position.opened_at)}
            </p>
          </button>
          <Badge variant={isOpen ? "positive" : "default"}>
            {position.status}
          </Badge>
        </div>

        <div className="grid grid-cols-2 gap-3 text-xs">
          <div>
            <p className="text-muted-foreground">Side</p>
            <p className={cn("mt-1 font-mono font-medium", sideClass(position.side))}>
              {position.side.toUpperCase()}
            </p>
          </div>
          <div>
            <p className="text-muted-foreground">Qty</p>
            <p className="mt-1 font-mono text-foreground">{position.quantity}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Entry</p>
            <p className="mt-1 font-mono text-foreground">{formatPrice(position.entry_price)}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Exit</p>
            <p className="mt-1 font-mono text-muted-foreground">{formatPrice(position.exit_price)}</p>
          </div>
          <div>
            <p className="text-muted-foreground">PnL</p>
            <div className="mt-1">
              <PnlCell pnl={position.pnl} status={position.status} />
            </div>
          </div>
        </div>

        {isOpen && (
          <Button variant="danger" size="sm" onClick={onExit} className="w-full">
            Exit Position
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

function CompactPositionList({
  positions,
  isLoading,
  maxHeight,
  onViewMarket,
  onExit,
}: {
  positions: PaperPositionRead[];
  isLoading: boolean;
  maxHeight?: string;
  onViewMarket: (ticker: string) => void;
  onExit: (position: PaperPositionRead) => void;
}) {
  const { formatPrice } = usePriceDisplay();

  return (
    <div className="h-full overflow-y-auto pr-1" style={maxHeight ? { maxHeight } : undefined}>
      <div className="space-y-2">
        {isLoading
          ? Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="rounded-lg border border-border bg-surface px-3 py-3">
                <Skeleton className="h-4 w-28" />
                <Skeleton className="mt-2 h-3 w-20" />
              </div>
            ))
          : positions.length === 0
            ? (
              <div className="flex h-full min-h-24 items-center justify-center text-center text-xs text-muted-foreground">
                No paper positions yet
              </div>
            )
            : positions.map((position) => (
                <button
                  key={position.id}
                  className="flex w-full items-start justify-between gap-3 rounded-lg border border-border bg-surface px-3 py-3 text-left transition-colors duration-[120ms] hover:bg-surface-hover"
                  onClick={() => onViewMarket(position.ticker)}
                >
                  <div className="min-w-0">
                    <p className="truncate font-mono text-xs text-accent">{position.ticker}</p>
                    <div className="mt-1 flex flex-wrap items-center gap-2">
                      <span className={cn("font-mono text-xs font-medium", sideClass(position.side))}>
                        {position.side.toUpperCase()}
                      </span>
                      <span className="font-mono text-xs text-muted-foreground">
                        {position.quantity} @ {formatPrice(position.entry_price)}
                      </span>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <PnlCell pnl={position.pnl} status={position.status} />
                    {position.status === "open" && (
                      <Button
                        variant="ghost"
                        size="xs"
                        onClick={(event) => {
                          event.stopPropagation();
                          onExit(position);
                        }}
                      >
                        Exit
                      </Button>
                    )}
                  </div>
                </button>
              ))}
      </div>
    </div>
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
  if (error) {
    return (
      <div className="flex h-24 items-center justify-center text-xs text-negative">
        Failed to load positions.
      </div>
    );
  }

  return (
    <>
      {maxHeight ? (
        <CompactPositionList
          positions={positions}
          isLoading={isLoading}
          maxHeight={maxHeight}
          onViewMarket={setSelectedTicker}
          onExit={setExitingPosition}
        />
      ) : (
        <>
          <div className="space-y-3 lg:hidden">
            {isLoading
              ? Array.from({ length: 4 }).map((_, index) => (
                  <Card key={index} className="bg-surface-hover shadow-none">
                    <CardContent className="space-y-3 px-4 py-4">
                      <Skeleton className="h-4 w-40" />
                      <div className="grid grid-cols-2 gap-3">
                        <Skeleton className="h-10 w-full" />
                        <Skeleton className="h-10 w-full" />
                        <Skeleton className="h-10 w-full" />
                        <Skeleton className="h-10 w-full" />
                      </div>
                    </CardContent>
                  </Card>
                ))
              : positions.length === 0
                ? (
                  <div className="flex h-24 items-center justify-center rounded-xl border border-border bg-surface text-center text-xs text-muted-foreground">
                    No paper positions yet
                  </div>
                )
                : positions.map((position) => (
                    <PositionCard
                      key={position.id}
                      position={position}
                      onViewMarket={() => setSelectedTicker(position.ticker)}
                      onExit={() => setExitingPosition(position)}
                    />
                  ))}
          </div>

          <div className="hidden lg:block">
            <div className="overflow-x-auto">
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
          </div>
        </>
      )}

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
