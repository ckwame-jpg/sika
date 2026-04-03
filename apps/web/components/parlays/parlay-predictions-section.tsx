"use client";

import useSWR from "swr";
import { fetchParlayPredictionSummary, fetchParlayPredictions, keys } from "@/lib/api";
import type { ParlayPredictionRead, ParlayPredictionSummaryRead } from "@/lib/types";
import { Badge, SportBadge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn, edgeClass, fmtContractPnl, fmtDatetime, fmtEdge, fmtPercent } from "@/lib/utils";
import { parseParlayLegCount } from "@/components/parlays/parlay-filter-controls";

function settlementVariant(status: string): "positive" | "warning" | "default" {
  if (status === "settled") return "positive";
  if (status === "pending" || status === "unresolved") return "warning";
  return "default";
}

function outcomeVariant(outcome: string): "positive" | "negative" | "warning" | "default" {
  if (outcome === "won") return "positive";
  if (outcome === "lost") return "negative";
  if (outcome === "push") return "warning";
  return "default";
}

function sportScopeLabel(value: string) {
  if (value === "MIXED") return "NBA + MLB";
  return value;
}

function ParlaySummaryCards({ summary }: { summary: ParlayPredictionSummaryRead }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
      <Card className="bg-surface-hover shadow-none">
        <CardContent className="px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Total</p>
          <p className="mt-1 font-mono text-lg text-foreground">{summary.total_predictions}</p>
          <p className="text-xs text-muted-foreground">{summary.settled_predictions} settled</p>
        </CardContent>
      </Card>
      <Card className="bg-surface-hover shadow-none">
        <CardContent className="px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Pending</p>
          <p className="mt-1 font-mono text-lg text-foreground">{summary.pending_predictions}</p>
          <p className="text-xs text-muted-foreground">{summary.unresolved_predictions} unresolved</p>
        </CardContent>
      </Card>
      <Card className="bg-surface-hover shadow-none">
        <CardContent className="px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Win Rate</p>
          <p
            className={cn(
              "mt-1 font-mono text-lg",
              summary.win_rate == null
                ? "text-muted-foreground"
                : summary.win_rate >= 0.55
                  ? "text-positive"
                  : summary.win_rate >= 0.45
                    ? "text-warning"
                    : "text-negative",
            )}
          >
            {fmtPercent(summary.win_rate)}
          </p>
          <p className="text-xs text-muted-foreground">
            {summary.won_predictions}W / {summary.lost_predictions}L / {summary.cancelled_predictions}C
          </p>
        </CardContent>
      </Card>
      <Card className="bg-surface-hover shadow-none">
        <CardContent className="px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Avg Edge</p>
          <p className="mt-1 font-mono text-lg text-foreground">
            {summary.average_edge != null ? fmtEdge(summary.average_edge) : "—"}
          </p>
        </CardContent>
      </Card>
      <Card className="bg-surface-hover shadow-none">
        <CardContent className="px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Avg PnL</p>
          <p
            className={cn(
              "mt-1 font-mono text-lg",
              summary.average_realized_pnl == null
                ? "text-muted-foreground"
                : summary.average_realized_pnl >= 0
                  ? "text-positive"
                  : "text-negative",
            )}
          >
            {fmtContractPnl(summary.average_realized_pnl)}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function ParlayPredictionRow({ row }: { row: ParlayPredictionRead }) {
  return (
    <TableRow>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {fmtDatetime(row.captured_at)}
      </TableCell>
      <TableCell>
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-foreground">{row.leg_count} legs</span>
            <Badge variant="outline">{sportScopeLabel(row.sport_scope)}</Badge>
          </div>
          <div className="space-y-1">
            {row.legs.map((leg) => (
              <p key={`${row.id}-${leg.leg_index}`} className="truncate text-xs text-muted-foreground">
                <span className="mr-1 font-mono text-foreground">{leg.leg_index}.</span>
                {leg.side.toUpperCase()} {leg.market_title}
              </p>
            ))}
          </div>
        </div>
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          {row.participating_sports.map((sport) => (
            <SportBadge key={`${row.id}-${sport}`} sport={sport} />
          ))}
        </div>
      </TableCell>
      <TableCell className="font-mono text-xs text-foreground">
        {row.american_odds}
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {fmtPercent(row.combined_model_probability)}
      </TableCell>
      <TableCell>
        <span className={cn("font-mono text-xs font-medium", edgeClass(row.edge))}>
          {fmtEdge(row.edge)}
        </span>
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {fmtPercent(row.confidence)}
      </TableCell>
      <TableCell>
        <Badge variant={settlementVariant(row.settlement_status)}>
          {row.settlement_status}
        </Badge>
      </TableCell>
      <TableCell>
        <Badge variant={outcomeVariant(row.prediction_outcome)}>
          {row.prediction_outcome}
        </Badge>
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {fmtDatetime(row.settled_at)}
      </TableCell>
    </TableRow>
  );
}

export function ParlayPredictionsSection({
  sportScope,
  legCount,
}: {
  sportScope: string;
  legCount: string;
}) {
  const numericLegCount = parseParlayLegCount(legCount);
  const { data: summary, isLoading: summaryLoading } = useSWR<ParlayPredictionSummaryRead>(
    keys.parlayPredictionSummary(sportScope, numericLegCount),
    () => fetchParlayPredictionSummary(sportScope, numericLegCount),
    { refreshInterval: 30_000 },
  );
  const { data, isLoading, error } = useSWR<ParlayPredictionRead[]>(
    keys.parlayPredictions(sportScope, numericLegCount, 100),
    () => fetchParlayPredictions(sportScope, numericLegCount, 100),
    { refreshInterval: 30_000 },
  );

  const items = data ?? [];

  return (
    <div className="space-y-4">
      {summaryLoading ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-20 w-full rounded-xl" />
          ))}
        </div>
      ) : summary ? (
        <ParlaySummaryCards summary={summary} />
      ) : null}

      <Card>
        <CardHeader className="flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle>Parlay Ledger</CardTitle>
            <CardDescription>
              Stored parlay predictions and settlement outcomes for NBA, MLB, and mixed combinations.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent className="pt-0">
          {error ? (
            <div className="flex h-24 items-center justify-center text-xs text-negative">
              Failed to load parlay predictions.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-32">Captured</TableHead>
                    <TableHead>Legs</TableHead>
                    <TableHead className="w-32">Sports</TableHead>
                    <TableHead className="w-24">Odds</TableHead>
                    <TableHead className="w-24">Model</TableHead>
                    <TableHead className="w-20">Edge</TableHead>
                    <TableHead className="w-24">Confidence</TableHead>
                    <TableHead className="w-28">Settlement</TableHead>
                    <TableHead className="w-24">Outcome</TableHead>
                    <TableHead className="w-32">Settled At</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {isLoading
                    ? Array.from({ length: 6 }).map((_, index) => (
                        <SkeletonRow key={index} cols={10} />
                      ))
                    : items.length === 0
                      ? (
                        <TableRow>
                          <TableCell colSpan={10} className="py-10 text-center text-xs text-muted-foreground">
                            No parlay predictions matched the current filters.
                          </TableCell>
                        </TableRow>
                      )
                      : items.map((row) => (
                          <ParlayPredictionRow key={row.id} row={row} />
                        ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
