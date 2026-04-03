"use client";

import useSWR, { mutate } from "swr";
import {
  fetchPredictions,
  fetchPredictionSummary,
  triggerPredictionSettlement,
  keys,
} from "@/lib/api";
import type { PredictionRead, PredictionSummaryRead } from "@/lib/types";
import { ViewSwitch, useViewQueryParam } from "@/components/filters/view-switch";
import { ParlayFilterControls } from "@/components/parlays/parlay-filter-controls";
import { ParlayPredictionsSection } from "@/components/parlays/parlay-predictions-section";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Badge, SportBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
import { cn, fmtContractPnl, fmtDatetime, fmtEdge, fmtPercent } from "@/lib/utils";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { SportFilterSelect, useSportQueryParam } from "@/components/filters/sport-filter-select";
import { usePriceDisplay } from "@/lib/price-display";

function outcomeVariant(
  outcome: string,
): "positive" | "negative" | "warning" | "default" {
  if (outcome === "won") return "positive";
  if (outcome === "lost") return "negative";
  if (outcome === "push") return "warning";
  return "default";
}

function settlementVariant(
  status: string,
): "positive" | "warning" | "default" {
  if (status === "settled") return "positive";
  if (status === "pending" || status === "unresolved") return "warning";
  return "default";
}

function SummaryCards({ summary }: { summary: PredictionSummaryRead }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
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
          <p className={cn(
            "mt-1 font-mono text-lg",
            summary.win_rate == null
              ? "text-muted-foreground"
              : summary.win_rate >= 0.55
                ? "text-positive"
                : summary.win_rate >= 0.45
                  ? "text-warning"
                  : "text-negative",
          )}>
            {fmtPercent(summary.win_rate)}
          </p>
          <p className="text-xs text-muted-foreground">
            {summary.won_predictions}W / {summary.lost_predictions}L / {summary.push_predictions}P
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
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Avg Confidence</p>
          <p className="mt-1 font-mono text-lg text-foreground">
            {fmtPercent(summary.average_confidence)}
          </p>
        </CardContent>
      </Card>
      <Card className="bg-surface-hover shadow-none">
        <CardContent className="px-3 py-3">
          <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Avg PnL</p>
          <p className={cn(
            "mt-1 font-mono text-lg",
            summary.average_realized_pnl == null
              ? "text-muted-foreground"
              : summary.average_realized_pnl >= 0
                ? "text-positive"
                : "text-negative",
          )}>
            {fmtContractPnl(summary.average_realized_pnl)}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function PredictionRow({ row }: { row: PredictionRead }) {
  const { formatPrice } = usePriceDisplay();
  const label = row.subject_name
    ? `${row.subject_name}${row.stat_key ? ` · ${row.stat_key}` : ""}${row.threshold != null ? ` ${row.threshold}` : ""}`
    : row.market_title;

  return (
    <TableRow>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {fmtDatetime(row.captured_at)}
      </TableCell>
      <TableCell>
        <div className="max-w-[280px]">
          <p className="truncate text-sm text-foreground">{label}</p>
          {row.subject_name && (
            <p className="truncate text-xs text-muted-foreground">{row.market_title}</p>
          )}
        </div>
      </TableCell>
      <TableCell>
        {row.sport_key ? (
          <SportBadge sport={row.sport_key} />
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </TableCell>
      <TableCell>
        <span className={cn(
          "font-mono text-xs font-medium",
          row.side.toLowerCase() === "yes" ? "text-positive" : "text-negative",
        )}>
          {row.side.toUpperCase()}
        </span>
        <span className="ml-1 font-mono text-xs text-muted-foreground">
          {formatPrice(row.suggested_price)}
        </span>
      </TableCell>
      <TableCell className="font-mono text-xs">
        {fmtEdge(row.edge)}
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

export function PredictionsDesk() {
  const { sport } = useSportQueryParam();
  const { view, setView } = useViewQueryParam();
  const [family, setFamily] = useState("all");
  const [statKey, setStatKey] = useState("");
  const [outcome, setOutcome] = useState("all");
  const [capturedFrom, setCapturedFrom] = useState("");
  const [capturedTo, setCapturedTo] = useState("");
  const [settling, setSettling] = useState(false);
  const [parlaySportScope, setParlaySportScope] = useState("all");
  const [parlayLegCount, setParlayLegCount] = useState("all");

  const filterArgs = {
    sport,
    market_family: family !== "all" ? family : undefined,
    stat_key: statKey || undefined,
    outcome: outcome !== "all" ? outcome : undefined,
    captured_from: capturedFrom || undefined,
    captured_to: capturedTo || undefined,
  };

  const hasFilters = Boolean(
    sport ||
    family !== "all" ||
    statKey ||
    outcome !== "all" ||
    capturedFrom ||
    capturedTo,
  );

  const { data: predictions, isLoading: predsLoading, error: predsError } = useSWR<PredictionRead[]>(
    view === "singles" ? keys.predictions(filterArgs) : null,
    () => fetchPredictions({ ...filterArgs, limit: 200 }),
    { refreshInterval: 30_000 },
  );

  const { data: summary, isLoading: summaryLoading } = useSWR<PredictionSummaryRead>(
    view === "singles" ? keys.predictionSummary(filterArgs) : null,
    () => fetchPredictionSummary(filterArgs),
    { refreshInterval: 30_000 },
  );

  async function handleSettle() {
    setSettling(true);
    try {
      await triggerPredictionSettlement();
      await Promise.all([
        mutate((key) => typeof key === "string" && key.startsWith("/predictions")),
        mutate((key) => typeof key === "string" && key.startsWith("/predictions/summary")),
        mutate((key) => typeof key === "string" && key.startsWith("/parlays/predictions")),
      ]);
    } catch {
      /* ignore */
    } finally {
      setTimeout(() => setSettling(false), 1200);
    }
  }

  const predictionErrorMessage = predsError instanceof Error
    ? predsError.message
    : "Unknown error";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3">
        <ViewSwitch view={view} onChange={setView} />
        {view === "singles" ? (
          <>
            <SportFilterSelect triggerClassName="h-8 w-[140px]" />

            <Select value={family} onValueChange={setFamily}>
              <SelectTrigger className="h-8 w-[160px]">
                <SelectValue placeholder="All families" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All families</SelectItem>
                <SelectItem value="player_prop">Player props</SelectItem>
                <SelectItem value="winner">Winners</SelectItem>
              </SelectContent>
            </Select>

            <Input
              value={statKey}
              onChange={(event) => setStatKey(event.target.value)}
              placeholder="Stat key (e.g. points)"
              className="h-8 w-[180px]"
            />

            <Select value={outcome} onValueChange={setOutcome}>
              <SelectTrigger className="h-8 w-[140px]">
                <SelectValue placeholder="All outcomes" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All outcomes</SelectItem>
                <SelectItem value="pending">Pending</SelectItem>
                <SelectItem value="won">Won</SelectItem>
                <SelectItem value="lost">Lost</SelectItem>
                <SelectItem value="push">Push</SelectItem>
                <SelectItem value="cancelled">Cancelled</SelectItem>
              </SelectContent>
            </Select>

            <Input
              type="date"
              value={capturedFrom}
              onChange={(event) => setCapturedFrom(event.target.value)}
              className="h-8 w-[150px]"
              title="Captured from"
            />
            <Input
              type="date"
              value={capturedTo}
              onChange={(event) => setCapturedTo(event.target.value)}
              className="h-8 w-[150px]"
              title="Captured to"
            />
          </>
        ) : (
          <ParlayFilterControls
            sportScope={parlaySportScope}
            onSportScopeChange={setParlaySportScope}
            legCount={parlayLegCount}
            onLegCountChange={setParlayLegCount}
          />
        )}

        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-muted-foreground">
            {view === "singles" && predictions != null ? `${predictions.length} predictions · ` : ""}30s refresh
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="gap-2 text-muted-foreground"
            onClick={handleSettle}
            disabled={settling}
          >
            <RefreshCw size={13} className={cn(settling && "animate-spin")} />
            Settle predictions
          </Button>
        </div>
      </div>

      {view === "singles" ? (
        <>
          {summaryLoading ? (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} className="h-20 w-full rounded-xl" />
              ))}
            </div>
          ) : summary ? (
            <SummaryCards summary={summary} />
          ) : null}

          <Card>
            <CardHeader className="flex-col items-start gap-1 border-none">
              <CardTitle>Prediction Ledger</CardTitle>
            </CardHeader>
            <CardContent className="pb-0">
              {predsError ? (
                <div className="flex h-24 items-center justify-center text-center text-xs text-negative">
                  Failed to load predictions: {predictionErrorMessage}
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead className="w-32">Captured</TableHead>
                        <TableHead>Market / Subject</TableHead>
                        <TableHead className="w-20">Sport</TableHead>
                        <TableHead className="w-24">Side / Price</TableHead>
                        <TableHead className="w-20">Edge</TableHead>
                        <TableHead className="w-24">Confidence</TableHead>
                        <TableHead className="w-28">Settlement</TableHead>
                        <TableHead className="w-24">Outcome</TableHead>
                        <TableHead className="w-32">Settled At</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {predsLoading
                        ? Array.from({ length: 8 }).map((_, index) => (
                            <SkeletonRow key={index} cols={9} />
                          ))
                        : (predictions ?? []).length === 0
                          ? (
                            <TableRow>
                              <TableCell
                                colSpan={9}
                                className="py-10 text-center text-xs text-muted-foreground"
                              >
                                {hasFilters
                                  ? "No predictions matched the current filters."
                                  : "No predictions yet. Run a refresh to emit picks."}
                              </TableCell>
                            </TableRow>
                          )
                          : (predictions ?? []).map((row) => (
                              <PredictionRow key={row.id} row={row} />
                            ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      ) : (
        <ParlayPredictionsSection
          sportScope={parlaySportScope}
          legCount={parlayLegCount}
        />
      )}
    </div>
  );
}
