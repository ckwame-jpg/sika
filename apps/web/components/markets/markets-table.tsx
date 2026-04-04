"use client";

import { useState } from "react";
import useSWR from "swr";
import { fetchMarkets, keys } from "@/lib/api";
import type { MarketListRead } from "@/lib/types";
import type { RecommendationViewMode } from "@/components/filters/quality-filter-select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { MarketDetailSheet } from "./market-detail-sheet";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
import { Badge, SportBadge } from "@/components/ui/badge";
import { fmtDatetime, fmtEdge, fmtRelative } from "@/lib/utils";
import { cn, edgeClass } from "@/lib/utils";
import { usePriceDisplay } from "@/lib/price-display";
import { matchesRecommendationViewMode } from "@/lib/recommendation-quality";

interface MarketsTableProps {
  sport?: string;
  family?: string;
  status?: string;
  search?: string;
  qualityMode?: RecommendationViewMode;
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

function MarketCard({
  market,
  onClick,
}: {
  market: MarketListRead;
  onClick: () => void;
}) {
  const { formatPrice } = usePriceDisplay();
  const recommendation = market.latest_recommendation;
  const snapshot = market.latest_snapshot;
  const subtitle = market.subject_name ?? market.event_name ?? market.subtitle ?? "Market detail";

  return (
    <div
      role="button"
      tabIndex={0}
      className="w-full rounded-xl border border-border bg-surface p-4 text-left transition-colors duration-[120ms] hover:border-border-bright hover:bg-surface-hover"
      onClick={onClick}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick();
        }
      }}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {market.sport_key ? <SportBadge sport={market.sport_key} /> : null}
            {market.market_family ? <Badge variant="outline">{market.market_family}</Badge> : null}
            <Badge variant={market.status === "open" ? "positive" : "default"}>
              {market.status}
            </Badge>
            <span className="font-mono text-xs text-muted-foreground">
              {fmtRelative(market.close_time)}
            </span>
          </div>
          <div>
            <p className="text-sm font-medium text-foreground">{market.title}</p>
            <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>
            <p className="mt-1 font-mono text-[11px] text-muted-foreground">{market.ticker}</p>
          </div>
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3">
        <div>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Last</p>
          <p className="mt-1 font-mono text-sm text-foreground">{formatPrice(snapshot?.last_price)}</p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Yes Ask</p>
          <p className="mt-1 font-mono text-sm text-foreground">{formatPrice(snapshot?.yes_ask)}</p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Edge</p>
          <p className={cn("mt-1 font-mono text-sm font-medium", recommendation ? edgeClass(recommendation.edge) : "text-muted-foreground")}>
            {recommendation ? fmtEdge(recommendation.edge) : "—"}
          </p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Closes</p>
          <p className="mt-1 font-mono text-sm text-foreground">{fmtDatetime(market.close_time)}</p>
        </div>
      </div>
    </div>
  );
}

export function MarketsTable({
  sport,
  family,
  status,
  search,
  qualityMode = "balanced",
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

  const markets = (data ?? []).filter((market) => {
    if (qualityMode === "balanced") {
      return true;
    }
    return market.latest_recommendation ? matchesRecommendationViewMode(market.latest_recommendation, qualityMode) : false;
  });

  return (
    <>
      <div className="space-y-3 lg:hidden">
        {isLoading
          ? Array.from({ length: 6 }).map((_, index) => (
              <div key={index} className="rounded-xl border border-border bg-surface p-4">
                <Skeleton className="h-4 w-24" />
                <Skeleton className="mt-3 h-4 w-3/4" />
                <Skeleton className="mt-2 h-3 w-1/2" />
                <div className="mt-4 grid grid-cols-2 gap-3">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                </div>
              </div>
            ))
          : markets.length === 0
            ? (
                <div className="flex h-24 items-center justify-center rounded-xl border border-border bg-surface text-center text-xs text-muted-foreground">
                  No markets matched the current filters.
                </div>
              )
            : markets.map((market) => (
                <MarketCard
                  key={market.ticker}
                  market={market}
                  onClick={() => setSelectedTicker(market.ticker)}
                />
              ))}
      </div>

      <div className="hidden overflow-x-auto lg:block">
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
