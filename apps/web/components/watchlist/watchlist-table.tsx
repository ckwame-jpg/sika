"use client";

import { useState } from "react";
import useSWR from "swr";
import { fetchWatchlist, keys } from "@/lib/api";
import type { RecommendationRead } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SportBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SkeletonRow } from "@/components/ui/skeleton";
import { MarketDetailSheet } from "@/components/markets/market-detail-sheet";
import { fmtEdge, fmtRelative, edgeClass, sideClass } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { usePriceDisplay } from "@/lib/price-display";

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  return (
    <div className="flex items-center gap-2">
      <div className="h-1 w-16 overflow-hidden rounded-full bg-border">
        <div
          className={cn(
            "h-full rounded-full transition-all",
            pct >= 70 ? "bg-positive" : pct >= 50 ? "bg-warning" : "bg-muted-foreground",
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="font-mono text-xs text-muted-foreground">{pct}%</span>
    </div>
  );
}

function RecommendationRow({
  rec,
  onClick,
  onTrade,
}: {
  rec: RecommendationRead;
  onClick: () => void;
  onTrade: () => void;
}) {
  const { formatPrice } = usePriceDisplay();

  return (
    <TableRow className="cursor-pointer" onClick={onClick}>
      <TableCell>
        <div className="max-w-64">
          <p className="truncate text-sm text-foreground">{rec.market_title}</p>
          {rec.subject_name && (
            <p className="truncate text-xs text-muted-foreground">
              {rec.subject_name}
              {rec.subject_team && ` · ${rec.subject_team}`}
            </p>
          )}
        </div>
      </TableCell>
      <TableCell>
        {rec.sport_key && <SportBadge sport={rec.sport_key} />}
      </TableCell>
      <TableCell>
        <div className="font-mono text-xs text-foreground">{rec.ticker}</div>
      </TableCell>
      <TableCell>
        <span className={cn("font-mono text-xs font-medium", sideClass(rec.side))}>
          {rec.side.toUpperCase()}
        </span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{formatPrice(rec.suggested_price)}</span>
      </TableCell>
      <TableCell>
        <span className={cn("font-mono text-xs font-medium", edgeClass(rec.edge))}>
          {fmtEdge(rec.edge)}
        </span>
      </TableCell>
      <TableCell>
        <ConfidenceBar value={rec.confidence} />
      </TableCell>
      <TableCell>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="secondary"
              size="xs"
              onClick={(event) => {
                event.stopPropagation();
                onTrade();
              }}
            >
              Trade
            </Button>
          </TooltipTrigger>
          <TooltipContent className="max-w-72 space-y-1">
            <p>Route this pick to paper or demo.</p>
            <p className="text-muted-foreground">{rec.rationale}</p>
          </TooltipContent>
        </Tooltip>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs text-muted-foreground">
          {fmtRelative(rec.captured_at)}
        </span>
      </TableCell>
    </TableRow>
  );
}

interface WatchlistTableProps {
  sport?: string;
  limit?: number;
  maxHeight?: string;
}

export function WatchlistTable({
  sport,
  limit = 50,
  maxHeight,
}: WatchlistTableProps) {
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [tradeRec, setTradeRec] = useState<RecommendationRead | null>(null);

  const { data, isLoading, error } = useSWR<RecommendationRead[]>(
    keys.watchlist(sport, limit),
    () => fetchWatchlist(sport, limit),
    { refreshInterval: 30_000 },
  );

  if (error) {
    return (
      <div className="flex h-24 items-center justify-center text-xs text-negative">
        Failed to load watchlist.
      </div>
    );
  }

  const items = data ?? [];
  const wrapperClassName = maxHeight ? "overflow-auto" : "overflow-x-auto";
  const wrapperStyle = maxHeight ? { maxHeight } : undefined;

  return (
    <>
      <div className={wrapperClassName} style={wrapperStyle}>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Market</TableHead>
              <TableHead className="w-20">Sport</TableHead>
              <TableHead className="w-48">Ticker</TableHead>
              <TableHead className="w-14">Side</TableHead>
              <TableHead className="w-20">Price</TableHead>
              <TableHead className="w-20">Edge</TableHead>
              <TableHead className="w-32">Confidence</TableHead>
              <TableHead className="w-24">Action</TableHead>
              <TableHead className="w-24">Age</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading
              ? Array.from({ length: 8 }).map((_, index) => (
                  <SkeletonRow key={index} cols={9} />
                ))
              : items.length === 0
                ? (
                  <TableRow>
                    <TableCell colSpan={9} className="py-8 text-center text-xs text-muted-foreground">
                      No watchlist items found. Run a refresh to emit picks.
                    </TableCell>
                  </TableRow>
                )
                : items.map((rec) => (
                    <RecommendationRow
                      key={rec.id}
                      rec={rec}
                      onClick={() => setSelectedTicker(rec.ticker)}
                      onTrade={() => setTradeRec(rec)}
                    />
                  ))}
          </TableBody>
        </Table>
      </div>

      <MarketDetailSheet ticker={selectedTicker} onClose={() => setSelectedTicker(null)} />

      <TradeDialog
        open={tradeRec != null}
        onOpenChange={(open) => {
          if (!open) setTradeRec(null);
        }}
        defaults={tradeRec != null ? {
          destination: "paper",
          ticker: tradeRec.ticker,
          side: tradeRec.side.toLowerCase(),
          price: tradeRec.suggested_price,
        } : undefined}
        description={tradeRec != null
          ? `Route ${tradeRec.market_title} to paper or demo.`
          : "Choose whether to route this trade to paper or demo."}
      />
    </>
  );
}
