"use client";

import useSWR from "swr";
import { fetchParlayPredictionSummary, fetchParlayPredictions, keys } from "@/lib/api";
import type { ParlayPredictionRead, ParlayPredictionSummaryRead } from "@/lib/types";
import { Badge, SportBadge } from "@/components/ui/badge";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Sparkline } from "@/components/ui/sparkline";
import { cn, edgeClass, fmtContractPnl, fmtDatetime, fmtEdge, fmtPercent } from "@/lib/utils";
import { parseParlayLegCount } from "@/components/parlays/parlay-filter-controls";

function settlementPillClass(status: string): string {
  const key = status.toLowerCase();
  if (key === "settled" || key === "pending" || key === "unresolved") {
    return key;
  }
  return "";
}

function outcomePillClass(outcome: string): string {
  const key = outcome.toLowerCase();
  if (key === "won" || key === "lost" || key === "push" || key === "cancelled") {
    return key;
  }
  return "";
}

function sportScopeLabel(value: string) {
  if (value === "MIXED") return "NBA + MLB";
  return value;
}

interface KpiSpec {
  label: string;
  value: string;
  sub?: string;
  tone?: "pos" | "neg" | "warn";
  series: number[];
}

function KpiCard({ spec }: { spec: KpiSpec }) {
  return (
    <div className="trade-kpi">
      <div className="trade-kpi-orb" aria-hidden />
      <p className="trade-kpi-label">{spec.label}</p>
      <p className={cn("trade-kpi-value", spec.tone)}>{spec.value}</p>
      {spec.sub && <p className="trade-kpi-sub">{spec.sub}</p>}
      {spec.series.length >= 2 && (
        <Sparkline values={spec.series} width={120} height={16} className="trade-kpi-spark" />
      )}
    </div>
  );
}

const SPARK_BUCKETS = 14;

interface ParlaySeries {
  total: number[];
  pending: number[];
  winRate: number[];
  avgEdge: number[];
  avgPnl: number[];
}

const EMPTY_PARLAY_SERIES: ParlaySeries = {
  total: [],
  pending: [],
  winRate: [],
  avgEdge: [],
  avgPnl: [],
};

function mean(values: number[]): number | null {
  if (values.length === 0) return null;
  let sum = 0;
  for (const v of values) sum += v;
  return sum / values.length;
}

function buildParlaySeries(predictions: ParlayPredictionRead[]): ParlaySeries {
  if (predictions.length === 0) return EMPTY_PARLAY_SERIES;

  const sorted = [...predictions].sort(
    (a, b) => new Date(a.captured_at).getTime() - new Date(b.captured_at).getTime(),
  );
  const bucketSize = Math.max(1, Math.ceil(sorted.length / SPARK_BUCKETS));
  const series: ParlaySeries = {
    total: [],
    pending: [],
    winRate: [],
    avgEdge: [],
    avgPnl: [],
  };

  let lastWinRate = 0;
  let lastAvgPnl = 0;

  for (let i = 0; i < SPARK_BUCKETS; i++) {
    const upTo = Math.min((i + 1) * bucketSize, sorted.length);
    if (upTo === 0) continue;
    const cumulative = sorted.slice(0, upTo);
    const bucket = sorted.slice(i * bucketSize, upTo);

    series.total.push(cumulative.length);
    series.pending.push(
      cumulative.filter((p) => p.settlement_status.toLowerCase() === "pending").length,
    );

    const settled = cumulative.filter((p) => p.settlement_status.toLowerCase() === "settled");
    if (settled.length > 0) {
      const won = settled.filter((p) => p.prediction_outcome.toLowerCase() === "won").length;
      lastWinRate = won / settled.length;
      const pnls = settled.map((p) => p.realized_pnl).filter((v): v is number => v != null);
      lastAvgPnl = mean(pnls) ?? lastAvgPnl;
    }
    series.winRate.push(lastWinRate);
    series.avgPnl.push(lastAvgPnl);
    series.avgEdge.push(mean(bucket.map((p) => p.edge)) ?? 0);

    if (upTo === sorted.length) break;
  }

  return series;
}

function buildParlayKpis(
  summary: ParlayPredictionSummaryRead,
  series: ParlaySeries,
): KpiSpec[] {
  const winRateTone =
    summary.win_rate == null
      ? undefined
      : summary.win_rate >= 0.55
        ? "pos"
        : summary.win_rate >= 0.45
          ? "warn"
          : "neg";
  const pnlTone =
    summary.average_realized_pnl == null
      ? undefined
      : summary.average_realized_pnl >= 0
        ? "pos"
        : "neg";

  return [
    {
      label: "Total",
      value: String(summary.total_predictions),
      sub: `${summary.settled_predictions} settled`,
      series: series.total,
    },
    {
      label: "Pending",
      value: String(summary.pending_predictions),
      sub: `${summary.unresolved_predictions} unresolved`,
      series: series.pending,
    },
    {
      label: "Win Rate",
      value: fmtPercent(summary.win_rate),
      sub: `${summary.won_predictions}W / ${summary.lost_predictions}L / ${summary.cancelled_predictions}C`,
      tone: winRateTone,
      series: series.winRate,
    },
    {
      label: "Avg Edge",
      value: summary.average_edge != null ? fmtEdge(summary.average_edge) : "—",
      series: series.avgEdge,
    },
    {
      label: "Avg PnL",
      value: fmtContractPnl(summary.average_realized_pnl),
      tone: pnlTone,
      series: series.avgPnl,
    },
  ];
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
        <span className={cn("outcome-pill", settlementPillClass(row.settlement_status))}>
          {row.settlement_status}
        </span>
      </TableCell>
      <TableCell>
        <span className={cn("outcome-pill", outcomePillClass(row.prediction_outcome))}>
          {row.prediction_outcome}
        </span>
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
  const kpiSeries = buildParlaySeries(items);
  const summaryKpis = summary ? buildParlayKpis(summary, kpiSeries) : null;

  return (
    <div className="space-y-4">
      {summaryLoading ? (
        <div className="parlay-kpis">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-24 w-full rounded-xl" />
          ))}
        </div>
      ) : summaryKpis ? (
        <div className="parlay-kpis">
          {summaryKpis.map((spec) => (
            <KpiCard key={spec.label} spec={spec} />
          ))}
        </div>
      ) : null}

      <section className="cosmos-panel">
        <div className="cosmos-panel-head">
          <div className="cosmos-panel-head-text">
            <h2 className="cosmos-panel-title">Parlay Ledger</h2>
          </div>
        </div>
        <div className="cosmos-panel-body flush">
          {error ? (
            <div className="cosmos-table-empty">
              Failed to load parlay predictions.
            </div>
          ) : (
            <div className="cosmos-table-wrap">
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
                          <TableCell colSpan={10} className="cosmos-table-empty">
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
        </div>
      </section>
    </div>
  );
}
