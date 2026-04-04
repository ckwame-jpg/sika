"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR, { mutate } from "swr";
import { ArrowRight, ArrowUpDown, ChevronDown, ChevronUp, RefreshCw } from "lucide-react";
import { fetchWatchlist, fetchWatchlistDiagnostics, keys, triggerRefresh } from "@/lib/api";
import type { RecommendationRead, WatchlistDiagnosticsRead } from "@/lib/types";
import type { RecommendationViewMode } from "@/components/filters/quality-filter-select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge, SportBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
import { MarketDetailSheet } from "@/components/markets/market-detail-sheet";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { fmtEdge, fmtPercent, fmtRelative, edgeClass, sideClass, sportLabel } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { ENTRY_LABEL, RELIABILITY_LABEL, WIN_PROB_LABEL } from "@/lib/market-copy";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { usePriceDisplay } from "@/lib/price-display";
import { matchesRecommendationViewMode } from "@/lib/recommendation-quality";

type SortKey = "market" | "sport" | "ticker" | "side" | "entry" | "edge" | "winProb" | "age";
type SortDirection = "asc" | "desc";

const DEFAULT_SORT: { key: SortKey; direction: SortDirection } = {
  key: "edge",
  direction: "desc",
};

const SORT_OPTIONS: Array<{ key: SortKey; label: string }> = [
  { key: "market", label: "Market" },
  { key: "sport", label: "Sport" },
  { key: "ticker", label: "Ticker" },
  { key: "side", label: "Side" },
  { key: "entry", label: ENTRY_LABEL },
  { key: "edge", label: "Edge" },
  { key: "winProb", label: WIN_PROB_LABEL },
  { key: "age", label: "Age" },
];

function getRecommendationSortValue(rec: RecommendationRead, key: SortKey): number | string {
  switch (key) {
    case "market":
      return `${rec.display_market_title ?? rec.market_title} ${rec.subject_name ?? ""} ${rec.subject_team ?? ""}`.toLowerCase();
    case "sport":
      return (rec.sport_key ?? "").toLowerCase();
    case "ticker":
      return rec.ticker.toLowerCase();
    case "side":
      return rec.side.toLowerCase();
    case "entry":
      return rec.suggested_price;
    case "edge":
      return rec.edge;
    case "winProb":
      return rec.selected_side_probability ?? rec.confidence;
    case "age":
      return new Date(rec.captured_at).getTime();
  }
}

function compareRecommendations(
  left: RecommendationRead,
  right: RecommendationRead,
  key: SortKey,
  direction: SortDirection,
): number {
  const leftValue = getRecommendationSortValue(left, key);
  const rightValue = getRecommendationSortValue(right, key);

  let comparison = 0;
  if (typeof leftValue === "string" && typeof rightValue === "string") {
    comparison = leftValue.localeCompare(rightValue);
  } else {
    comparison = Number(leftValue) - Number(rightValue);
  }

  if (comparison === 0) {
    comparison = right.id - left.id;
  }
  return direction === "asc" ? comparison : comparison * -1;
}

function nextSortDirection(currentKey: SortKey, currentDirection: SortDirection, nextKey: SortKey): SortDirection {
  if (currentKey === nextKey) {
    return currentDirection === "asc" ? "desc" : "asc";
  }
  return nextKey === "market" || nextKey === "sport" || nextKey === "ticker" || nextKey === "side"
    ? "asc"
    : "desc";
}

function SortableTableHead({
  label,
  sortKey,
  activeKey,
  direction,
  onSort,
  className,
}: {
  label: string;
  sortKey: SortKey;
  activeKey: SortKey;
  direction: SortDirection;
  onSort: (key: SortKey) => void;
  className?: string;
}) {
  const isActive = activeKey === sortKey;

  return (
    <TableHead
      className={className}
      aria-sort={isActive ? (direction === "asc" ? "ascending" : "descending") : "none"}
    >
      <Button
        variant="ghost"
        size="sm"
        className="-ml-2 h-8 gap-1 px-2 text-[11px] uppercase tracking-[0.14em] text-muted-foreground hover:text-foreground"
        onClick={() => onSort(sortKey)}
      >
        <span>{label}</span>
        {isActive ? (
          direction === "asc" ? <ChevronUp size={14} /> : <ChevronDown size={14} />
        ) : (
          <ArrowUpDown size={14} className="opacity-60" />
        )}
      </Button>
    </TableHead>
  );
}

function MobileSortControls({
  sortKey,
  sortDirection,
  onSortKeyChange,
  onToggleDirection,
}: {
  sortKey: SortKey;
  sortDirection: SortDirection;
  onSortKeyChange: (key: SortKey) => void;
  onToggleDirection: () => void;
}) {
  return (
    <div className="grid grid-cols-[auto,minmax(0,1fr),auto] items-center gap-2 lg:hidden">
      <span className="text-xs text-muted-foreground">Sort</span>
      <Select value={sortKey} onValueChange={(value) => onSortKeyChange(value as SortKey)}>
        <SelectTrigger className="h-8 min-w-0 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {SORT_OPTIONS.map((option) => (
            <SelectItem key={option.key} value={option.key}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button
        variant="secondary"
        size="sm"
        className="h-8 w-8 px-0"
        onClick={onToggleDirection}
        aria-label={`Sort ${sortDirection === "asc" ? "ascending" : "descending"}`}
      >
        {sortDirection === "asc" ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </Button>
    </div>
  );
}

function PercentBar({ value }: { value: number }) {
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

function EmptyStateStat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface px-3 py-3">
      <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">{label}</p>
      <p className="mt-1 font-mono text-lg text-foreground">{value}</p>
      {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function EmptyWatchlistState({
  sport,
  diagnostics,
  refreshing,
  onRefresh,
}: {
  sport?: string;
  diagnostics?: WatchlistDiagnosticsRead;
  refreshing: boolean;
  onRefresh: () => Promise<void>;
}) {
  const filteredSportCount = sport
    ? (diagnostics?.latest_watchlist_counts_by_sport?.[sport] ?? 0)
    : diagnostics?.latest_recommendations_emitted ?? 0;
  const totalEmitted = diagnostics?.latest_recommendations_emitted ?? 0;
  const latestRun = diagnostics?.latest_refresh_run;

  let tone: "default" | "positive" | "warning" | "negative" = "default";
  let title = sport ? `No ${sportLabel(sport)} watchlist items` : "Watchlist is empty";
  let description = "No recommendations are currently available.";

  if (!diagnostics || !latestRun) {
    tone = refreshing ? "warning" : "default";
    title = refreshing ? "Refresh is running" : "No refresh has completed yet";
    description = refreshing
      ? "The backend is refreshing markets and re-scoring recommendations now."
      : "The watchlist is empty because the backend has not completed a refresh run yet.";
  } else if (refreshing || diagnostics.refresh_status === "queued" || diagnostics.refresh_status === "running") {
    tone = "warning";
    title = "Refresh is running";
    description = "The backend is refreshing markets and re-scoring recommendations now.";
  } else if (latestRun.status === "failed") {
    tone = "negative";
    title = "Latest refresh failed";
    description = diagnostics.refresh_error_message
      ? `${diagnostics.refresh_error_message} See Runs for technical details.`
      : "The backend did not complete the latest refresh, so no new recommendations were emitted. See Runs for technical details.";
  } else if (sport && totalEmitted > 0 && filteredSportCount === 0) {
    tone = "warning";
    description = `The latest refresh emitted ${totalEmitted} recommendations overall, but none for ${sportLabel(sport)}.`;
  } else if (latestRun.status === "completed" && totalEmitted === 0) {
    tone = "warning";
    title = "Latest refresh emitted 0 recommendations";
    description = "The watchlist is empty because the backend scored supported markets but nothing cleared the active thresholds and guardrails.";
  } else if (latestRun.status === "completed") {
    tone = "default";
    description = "The latest refresh completed, but the current watchlist filter returned no rows.";
  }

  const toneVariant = tone;

  return (
    <Card className="border-dashed">
      <CardContent className="space-y-4 px-4 py-4 sm:px-5 sm:py-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={toneVariant}>{title}</Badge>
              {latestRun && (
                <span className="text-xs text-muted-foreground">
                  Run #{latestRun.id} · {latestRun.status}
                </span>
              )}
            </div>
            <p className="max-w-2xl text-sm text-muted-foreground">{description}</p>
            {latestRun && (
              <p className="text-xs text-muted-foreground">
                Latest refresh {latestRun.finished_at ? `finished ${fmtRelative(latestRun.finished_at)}` : `started ${fmtRelative(latestRun.started_at)}`}
                {diagnostics?.last_successful_refresh_at ? ` · last success ${fmtRelative(diagnostics.last_successful_refresh_at)}` : ""}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-2 sm:flex-row">
            <Button
              variant="secondary"
              size="sm"
              className="justify-center"
              onClick={() => void onRefresh()}
              disabled={refreshing}
            >
              <RefreshCw size={13} className={cn(refreshing && "animate-spin")} />
              {refreshing ? "Refreshing" : "Run refresh"}
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <Link href="/runs" className="justify-center">
                Runs
                <ArrowRight size={13} />
              </Link>
            </Button>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <EmptyStateStat
            label="Supported Markets"
            value={String(diagnostics?.latest_supported_markets_kept ?? 0)}
            hint="Processed in the latest refresh"
          />
          <EmptyStateStat
            label={sport ? `${sportLabel(sport)} Picks` : "Recommendations"}
            value={String(filteredSportCount)}
            hint={sport && totalEmitted > 0 ? `${totalEmitted} total across all sports` : "Emitted in the latest refresh"}
          />
          <EmptyStateStat
            label="Min Edge"
            value={fmtEdge(diagnostics?.watchlist_min_edge ?? 0)}
            hint="Active API threshold"
          />
          <EmptyStateStat
            label="Min Confidence"
            value={fmtPercent(diagnostics?.watchlist_min_confidence ?? 0)}
            hint="Active API threshold"
          />
        </div>

        {diagnostics && Object.keys(diagnostics.latest_watchlist_counts_by_sport ?? {}).length > 0 && (
          <div className="space-y-2">
            <p className="text-xs uppercase tracking-[0.14em] text-muted-foreground">Latest By Sport</p>
            <div className="flex flex-wrap gap-2">
              {Object.entries(diagnostics.latest_watchlist_counts_by_sport).map(([sportKey, count]) => (
                <Badge key={sportKey} variant="outline" className="gap-2">
                  <span>{sportKey}</span>
                  <span className="font-mono text-foreground">{count}</span>
                </Badge>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
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
  const winProbability = rec.selected_side_probability ?? rec.confidence;
  const displayTitle = rec.display_market_title ?? rec.market_title;

  return (
    <TableRow className="cursor-pointer" onClick={onClick}>
      <TableCell>
        <div className="max-w-64">
          <div className="flex flex-wrap items-center gap-2">
            <p className="truncate text-sm text-foreground">{displayTitle}</p>
            {rec.source_badge_label && <Badge variant="outline">{rec.source_badge_label}</Badge>}
          </div>
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
        <div className="space-y-1">
          <PercentBar value={winProbability} />
          <p className="font-mono text-[11px] text-muted-foreground">
            {RELIABILITY_LABEL} {fmtPercent(rec.confidence)}
          </p>
        </div>
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

function RecommendationCard({
  rec,
  onClick,
  onTrade,
}: {
  rec: RecommendationRead;
  onClick: () => void;
  onTrade: () => void;
}) {
  const { formatPrice } = usePriceDisplay();
  const winProbability = rec.selected_side_probability ?? rec.confidence;
  const displayTitle = rec.display_market_title ?? rec.market_title;

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
            {rec.sport_key && <SportBadge sport={rec.sport_key} />}
            <Badge variant={rec.side.toLowerCase() === "yes" ? "positive" : "negative"}>
              {rec.side.toUpperCase()}
            </Badge>
            {rec.source_badge_label && <Badge variant="outline">{rec.source_badge_label}</Badge>}
            <span className="font-mono text-xs text-muted-foreground">
              {fmtRelative(rec.captured_at)}
            </span>
          </div>
          <div>
            <p className="text-sm font-medium text-foreground">{displayTitle}</p>
            <p className="mt-1 text-xs text-muted-foreground">{rec.event_name}</p>
            {rec.subject_name && (
              <p className="mt-1 text-xs text-muted-foreground">
                {rec.subject_name}
                {rec.subject_team && ` · ${rec.subject_team}`}
              </p>
            )}
          </div>
        </div>
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
      </div>

      <div className="mt-4 grid gap-3 grid-cols-3">
        <div>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">{ENTRY_LABEL}</p>
          <p className="mt-1 font-mono text-sm text-foreground">{formatPrice(rec.suggested_price)}</p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Edge</p>
          <p className={cn("mt-1 font-mono text-sm font-medium", edgeClass(rec.edge))}>{fmtEdge(rec.edge)}</p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">{WIN_PROB_LABEL}</p>
          <div className="mt-1">
            <PercentBar value={winProbability} />
            <p className="mt-1 font-mono text-[11px] text-muted-foreground">
              {RELIABILITY_LABEL} {fmtPercent(rec.confidence)}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

interface WatchlistTableProps {
  sport?: string;
  limit?: number;
  maxHeight?: string;
  qualityMode?: RecommendationViewMode;
}

export function WatchlistTable({
  sport,
  limit = 50,
  maxHeight,
  qualityMode = "balanced",
}: WatchlistTableProps) {
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [tradeRec, setTradeRec] = useState<RecommendationRead | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>(DEFAULT_SORT.key);
  const [sortDirection, setSortDirection] = useState<SortDirection>(DEFAULT_SORT.direction);

  const { data, isLoading, error } = useSWR<RecommendationRead[]>(
    keys.watchlist(sport, limit),
    () => fetchWatchlist(sport, limit),
    { refreshInterval: 30_000 },
  );
  const { data: diagnostics } = useSWR<WatchlistDiagnosticsRead>(
    keys.watchlistDiagnostics,
    fetchWatchlistDiagnostics,
    { refreshInterval: 15_000 },
  );

  if (error) {
    return (
      <div className="flex h-24 items-center justify-center text-xs text-negative">
        Failed to load watchlist.
      </div>
    );
  }

  const items = data ?? [];
  const filteredItems = items.filter((item) => matchesRecommendationViewMode(item, qualityMode));
  const sortedItems = [...filteredItems].sort((left, right) =>
    compareRecommendations(left, right, sortKey, sortDirection),
  );
  const wrapperClassName = maxHeight ? "overflow-auto" : "overflow-x-auto";
  const wrapperStyle = maxHeight ? { maxHeight } : undefined;

  function handleSort(nextKey: SortKey) {
    const nextDirection = nextSortDirection(sortKey, sortDirection, nextKey);
    setSortKey(nextKey);
    setSortDirection(nextDirection);
  }

  function handleMobileSortKeyChange(nextKey: SortKey) {
    setSortKey(nextKey);
    setSortDirection(
      nextKey === "market" || nextKey === "sport" || nextKey === "ticker" || nextKey === "side"
        ? "asc"
        : "desc",
    );
  }

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await triggerRefresh();
      await Promise.all([
        mutate((key) => typeof key === "string" && key.startsWith("/runs")),
        mutate((key) => typeof key === "string" && key.startsWith("/events")),
        mutate((key) => typeof key === "string" && key.startsWith("/watchlist")),
        mutate((key) => typeof key === "string" && key.startsWith("/markets")),
        mutate((key) => typeof key === "string" && key.startsWith("/positions")),
        mutate((key) => typeof key === "string" && key.startsWith("/predictions")),
        mutate((key) => typeof key === "string" && key.startsWith("/parlays")),
        mutate(keys.watchlistDiagnostics),
        mutate("/health"),
      ]);
    } catch {
      /* ignore */
    } finally {
      setTimeout(() => setRefreshing(false), 1200);
    }
  }

  return (
    <>
      {isLoading ? (
        <>
          <div className="space-y-3 lg:hidden">
            {Array.from({ length: 4 }).map((_, index) => (
              <div key={index} className="rounded-xl border border-border bg-surface p-4">
                <Skeleton className="h-4 w-24" />
                <Skeleton className="mt-3 h-4 w-3/4" />
                <Skeleton className="mt-2 h-3 w-1/2" />
                <div className="mt-4 grid grid-cols-3 gap-3">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                </div>
              </div>
            ))}
          </div>
          <div className={cn(wrapperClassName, "hidden lg:block")} style={wrapperStyle}>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead
                    label="Market"
                    sortKey="market"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                  />
                  <SortableTableHead
                    label="Sport"
                    sortKey="sport"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-20"
                  />
                  <SortableTableHead
                    label="Ticker"
                    sortKey="ticker"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-48"
                  />
                  <SortableTableHead
                    label="Side"
                    sortKey="side"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-14"
                  />
                  <SortableTableHead
                    label={ENTRY_LABEL}
                    sortKey="entry"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-20"
                  />
                  <SortableTableHead
                    label="Edge"
                    sortKey="edge"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-20"
                  />
                  <SortableTableHead
                    label={WIN_PROB_LABEL}
                    sortKey="winProb"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-32"
                  />
                  <TableHead className="w-24">Action</TableHead>
                  <SortableTableHead
                    label="Age"
                    sortKey="age"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-24"
                  />
                </TableRow>
              </TableHeader>
              <TableBody>
                {Array.from({ length: 8 }).map((_, index) => (
                  <SkeletonRow key={index} cols={9} />
                ))}
              </TableBody>
            </Table>
          </div>
        </>
      ) : items.length === 0 ? (
        <EmptyWatchlistState
          sport={sport}
          diagnostics={diagnostics}
          refreshing={refreshing || diagnostics?.refresh_status === "queued" || diagnostics?.refresh_status === "running"}
          onRefresh={handleRefresh}
        />
      ) : filteredItems.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="space-y-2 px-4 py-4 sm:px-5 sm:py-5">
            <Badge variant="warning">No {qualityMode === "quality" ? "quality-filtered" : "balanced"} picks</Badge>
            <p className="text-sm text-muted-foreground">
              The backend emitted recommendations, but none matched the current {qualityMode === "quality" ? "Quality" : "Balanced"} view.
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          <MobileSortControls
            sortKey={sortKey}
            sortDirection={sortDirection}
            onSortKeyChange={handleMobileSortKeyChange}
            onToggleDirection={() =>
              setSortDirection((current) => (current === "asc" ? "desc" : "asc"))
            }
          />
          <div className="space-y-3 lg:hidden">
            {sortedItems.map((rec) => (
              <RecommendationCard
                key={rec.id}
                rec={rec}
                onClick={() => setSelectedTicker(rec.ticker)}
                onTrade={() => setTradeRec(rec)}
              />
            ))}
          </div>
          <div className={cn(wrapperClassName, "hidden lg:block")} style={wrapperStyle}>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead
                    label="Market"
                    sortKey="market"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                  />
                  <SortableTableHead
                    label="Sport"
                    sortKey="sport"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-20"
                  />
                  <SortableTableHead
                    label="Ticker"
                    sortKey="ticker"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-48"
                  />
                  <SortableTableHead
                    label="Side"
                    sortKey="side"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-14"
                  />
                  <SortableTableHead
                    label={ENTRY_LABEL}
                    sortKey="entry"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-20"
                  />
                  <SortableTableHead
                    label="Edge"
                    sortKey="edge"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-20"
                  />
                  <SortableTableHead
                    label={WIN_PROB_LABEL}
                    sortKey="winProb"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-32"
                  />
                  <TableHead className="w-24">Action</TableHead>
                  <SortableTableHead
                    label="Age"
                    sortKey="age"
                    activeKey={sortKey}
                    direction={sortDirection}
                    onSort={handleSort}
                    className="w-24"
                  />
                </TableRow>
              </TableHeader>
              <TableBody>
                {sortedItems.map((rec) => (
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
        </>
      )}

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
