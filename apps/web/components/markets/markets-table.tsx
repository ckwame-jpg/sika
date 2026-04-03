"use client";

import { useState } from "react";
import useSWR from "swr";
import { fetchMarkets, keys } from "@/lib/api";
import type { MarketListRead } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { MarketDetailSheet } from "./market-detail-sheet";
import { SkeletonRow } from "@/components/ui/skeleton";
import { Badge, SportBadge } from "@/components/ui/badge";
import { fmtDatetime, fmtEdge } from "@/lib/utils";
import { cn, edgeClass } from "@/lib/utils";
import { usePriceDisplay } from "@/lib/price-display";

interface MarketsTableProps {
  sport?: string;
  family?: string;
  status?: string;
  search?: string;
}

function MarketRow({
  market,
  onClick,
}: {
  market: MarketListRead;
  onClick: () => void;
}) {
  const { formatPrice } = usePriceDisplay();
  const recommendation = market.latest_recommendation;
  const snapshot = market.latest_snapshot;

  return (
    <TableRow className="cursor-pointer" onClick={onClick}>
      <TableCell>
        <div className="max-w-[360px]">
          <p className="truncate text-sm text-foreground">{market.title}</p>
          <p className="truncate text-xs text-muted-foreground">
            {market.subject_name ?? market.event_name ?? market.subtitle ?? "Market detail"}
          </p>
        </div>
      </TableCell>
      <TableCell>
        {market.sport_key ? <SportBadge sport={market.sport_key} /> : <span className="text-muted-foreground">—</span>}
      </TableCell>
      <TableCell className="font-mono text-xs text-accent">{market.ticker}</TableCell>
      <TableCell>
        {market.market_family ? <Badge variant="outline">{market.market_family}</Badge> : "—"}
      </TableCell>
      <TableCell className="font-mono text-xs">{formatPrice(snapshot?.last_price)}</TableCell>
      <TableCell className="font-mono text-xs">{formatPrice(snapshot?.yes_ask)}</TableCell>
      <TableCell>
        {recommendation ? (
          <span className={cn("font-mono text-xs font-medium", edgeClass(recommendation.edge))}>
            {fmtEdge(recommendation.edge)}
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        )}
      </TableCell>
      <TableCell>
        <Badge variant={market.status === "open" ? "positive" : "default"}>
          {market.status}
        </Badge>
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {fmtDatetime(market.close_time)}
      </TableCell>
    </TableRow>
  );
}

export function MarketsTable({
  sport,
  family,
  status,
  search,
}: MarketsTableProps) {
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const { data, isLoading, error } = useSWR<MarketListRead[]>(
    keys.markets({ sport, family, status, search, limit: 150 }),
    () => fetchMarkets({ sport, family, status, search, limit: 150 }),
    { refreshInterval: 30_000 },
  );

  if (error) {
    return (
      <div className="flex h-24 items-center justify-center text-xs text-negative">
        Failed to load markets.
      </div>
    );
  }

  const markets = data ?? [];

  return (
    <>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Market</TableHead>
              <TableHead className="w-20">Sport</TableHead>
              <TableHead className="w-48">Ticker</TableHead>
              <TableHead className="w-24">Family</TableHead>
              <TableHead className="w-20">Last</TableHead>
              <TableHead className="w-20">Yes Ask</TableHead>
              <TableHead className="w-20">Edge</TableHead>
              <TableHead className="w-24">Status</TableHead>
              <TableHead className="w-32">Closes</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading
              ? Array.from({ length: 8 }).map((_, index) => <SkeletonRow key={index} cols={9} />)
              : markets.length === 0
                ? (
                  <TableRow>
                    <TableCell colSpan={9} className="py-10 text-center text-xs text-muted-foreground">
                      No markets matched the current filters.
                    </TableCell>
                  </TableRow>
                )
                : markets.map((market) => (
                    <MarketRow
                      key={market.ticker}
                      market={market}
                      onClick={() => setSelectedTicker(market.ticker)}
                    />
                  ))}
          </TableBody>
        </Table>
      </div>

      <MarketDetailSheet ticker={selectedTicker} onClose={() => setSelectedTicker(null)} />
    </>
  );
}
