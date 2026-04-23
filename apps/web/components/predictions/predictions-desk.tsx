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
import { QualityFilterSelect, type RecommendationViewMode } from "@/components/filters/quality-filter-select";
import { ParlayFilterControls } from "@/components/parlays/parlay-filter-controls";
import { ParlayPredictionsSection } from "@/components/parlays/parlay-predictions-section";
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
import { Sparkline, randomWalk } from "@/components/ui/sparkline";
import { cn, fmtContractPnl, fmtDatetime, fmtEdge, fmtPercent } from "@/lib/utils";
import { EDGE_EXPLANATION, ENTRY_LABEL, RELIABILITY_LABEL, WIN_PROB_LABEL } from "@/lib/market-copy";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { SportFilterSelect, useSportQueryParam } from "@/components/filters/sport-filter-select";
import { usePriceDisplay } from "@/lib/price-display";
import { matchesRecommendationViewMode } from "@/lib/recommendation-quality";

function outcomePillClass(outcome: string): string {
  const key = outcome.toLowerCase();
  if (key === "won" || key === "lost" || key === "push" || key === "cancelled") {
    return key;
  }
  return "";
}

function settlementPillClass(status: string): string {
  const key = status.toLowerCase();
  if (key === "settled" || key === "pending" || key === "unresolved") {
    return key;
  }
  return "";
}

function seedFromString(value: string): number {
  let h = 0;
  for (let i = 0; i < value.length; i++) {
    h = (h * 31 + value.charCodeAt(i)) >>> 0;
  }
  return h || 1;
}

interface KpiSpec {
  label: string;
  value: string;
  sub?: string;
  tone?: "pos" | "neg" | "warn";
  trendUp: boolean;
}

function KpiCard({ spec }: { spec: KpiSpec }) {
  const seed = seedFromString(spec.label);
  const series = randomWalk(14, spec.trendUp, seed);
  return (
    <div className="trade-kpi">
      <div className="trade-kpi-orb" aria-hidden />
      <p className="trade-kpi-label">{spec.label}</p>
      <p className={cn("trade-kpi-value", spec.tone)}>{spec.value}</p>
      {spec.sub && <p className="trade-kpi-sub">{spec.sub}</p>}
      <Sparkline values={series} width={120} height={16} className="trade-kpi-spark" />
    </div>
  );
}

function buildSummaryKpis(summary: PredictionSummaryRead): KpiSpec[] {
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
      trendUp: true,
    },
    {
      label: "Pending",
      value: String(summary.pending_predictions),
      sub: `${summary.unresolved_predictions} unresolved`,
      trendUp: false,
    },
    {
      label: "Win Rate",
      value: fmtPercent(summary.win_rate),
      sub: `${summary.won_predictions}W / ${summary.lost_predictions}L / ${summary.push_predictions}P`,
      tone: winRateTone,
      trendUp: (summary.win_rate ?? 0) >= 0.5,
    },
    {
      label: "Avg Edge",
      value: summary.average_edge != null ? fmtEdge(summary.average_edge) : "—",
      trendUp: (summary.average_edge ?? 0) >= 0,
    },
    {
      label: "Avg Confidence",
      value: fmtPercent(summary.average_confidence),
      trendUp: true,
    },
    {
      label: "Avg PnL",
      value: fmtContractPnl(summary.average_realized_pnl),
      tone: pnlTone,
      trendUp: (summary.average_realized_pnl ?? 0) >= 0,
    },
  ];
}

function PredictionRow({ row }: { row: PredictionRead }) {
  const { formatPrice } = usePriceDisplay();
  const displayTitle = row.display_market_title ?? row.market_title;
  const winProbability = row.selected_side_probability ?? row.confidence;
  const label = row.subject_name
    ? `${row.subject_name}${row.stat_key ? ` · ${row.stat_key}` : ""}${row.threshold != null ? ` ${row.threshold}` : ""}`
    : displayTitle;

  return (
    <TableRow>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {fmtDatetime(row.captured_at)}
      </TableCell>
      <TableCell>
        <div className="max-w-[280px]">
          <p className="truncate text-sm text-foreground">{label}</p>
          <div className="flex flex-wrap items-center gap-2">
            {row.subject_name && (
              <p className="truncate text-xs text-muted-foreground">{displayTitle}</p>
            )}
            {row.source_badge_label && <Badge variant="outline">{row.source_badge_label}</Badge>}
          </div>
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
        <div className="space-y-1">
          <p>{fmtPercent(winProbability)}</p>
          <p className="text-[11px]">{RELIABILITY_LABEL} {fmtPercent(row.confidence)}</p>
        </div>
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

function PredictionCard({ row }: { row: PredictionRead }) {
  const { formatPrice } = usePriceDisplay();
  const displayTitle = row.display_market_title ?? row.market_title;
  const winProbability = row.selected_side_probability ?? row.confidence;
  const label = row.subject_name
    ? `${row.subject_name}${row.stat_key ? ` · ${row.stat_key}` : ""}${row.threshold != null ? ` ${row.threshold}` : ""}`
    : displayTitle;
  const sideTone = row.side.toLowerCase() === "yes" ? "pos" : "neg";

  return (
    <article className="pred-card">
      <div className="pred-card-head">
        <div className="min-w-0">
          <p className="pred-card-title truncate">{label}</p>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            {row.subject_name && (
              <p className="pred-card-sub truncate">{displayTitle}</p>
            )}
            {row.source_badge_label && <Badge variant="outline">{row.source_badge_label}</Badge>}
            {row.sport_key && <SportBadge sport={row.sport_key} />}
          </div>
        </div>
        <p className="pred-card-time">{fmtDatetime(row.captured_at)}</p>
      </div>

      <div className="pred-card-grid">
        <div>
          <p className="pred-card-stat-label">Side</p>
          <p className={cn("pred-card-stat-value", sideTone)}>{row.side.toUpperCase()}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">{ENTRY_LABEL}</p>
          <p className="pred-card-stat-value">{formatPrice(row.suggested_price)}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">Edge</p>
          <p className="pred-card-stat-value">{fmtEdge(row.edge)}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">{WIN_PROB_LABEL}</p>
          <p className="pred-card-stat-value">{fmtPercent(winProbability)}</p>
          <p className="pred-card-sub mt-1">
            {RELIABILITY_LABEL} {fmtPercent(row.confidence)}
          </p>
        </div>
      </div>

      <div className="pred-card-pills">
        <span className={cn("outcome-pill", settlementPillClass(row.settlement_status))}>
          {row.settlement_status}
        </span>
        <span className={cn("outcome-pill", outcomePillClass(row.prediction_outcome))}>
          {row.prediction_outcome}
        </span>
        <span className="pred-card-sub">Settled {fmtDatetime(row.settled_at)}</span>
      </div>
    </article>
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
  const [qualityMode, setQualityMode] = useState<RecommendationViewMode>("balanced");
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
  const filteredPredictions = (predictions ?? []).filter((item) => matchesRecommendationViewMode(item, qualityMode));
  const summaryKpis = summary ? buildSummaryKpis(summary) : null;

  return (
    <div className="flex flex-col gap-4">
      <div className="cosmos-toolbar">
        <ViewSwitch view={view} onChange={setView} />
        {view === "singles" ? (
          <>
            <SportFilterSelect triggerClassName="h-8 w-full text-xs sm:w-[140px]" />

            <Select value={family} onValueChange={setFamily}>
              <SelectTrigger className="h-8 w-full sm:w-[160px]">
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
              className="h-8 w-full sm:w-[180px]"
            />

            <Select value={outcome} onValueChange={setOutcome}>
              <SelectTrigger className="h-8 w-full sm:w-[140px]">
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
              className="h-8 w-full sm:w-[150px]"
              title="Captured from"
            />
            <Input
              type="date"
              value={capturedTo}
              onChange={(event) => setCapturedTo(event.target.value)}
              className="h-8 w-full sm:w-[150px]"
              title="Captured to"
            />
            <QualityFilterSelect
              value={qualityMode}
              onValueChange={setQualityMode}
              triggerClassName="h-8 w-full text-xs sm:w-[130px]"
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

        <div className="cosmos-toolbar-spacer">
          <span className="cosmos-toolbar-meta">
            {view === "singles" && predictions != null ? `${filteredPredictions.length} predictions · ` : ""}30s refresh
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
            <div className="pred-kpis">
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton key={index} className="h-24 w-full rounded-xl" />
              ))}
            </div>
          ) : summaryKpis ? (
            <div className="pred-kpis">
              {summaryKpis.map((spec) => (
                <KpiCard key={spec.label} spec={spec} />
              ))}
            </div>
          ) : null}

          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Prediction Ledger</h2>
                <p className="cosmos-panel-desc">{EDGE_EXPLANATION}</p>
              </div>
            </div>
            <div className="cosmos-panel-body flush">
              {predsError ? (
                <div className="rounded-xl border border-negative/30 bg-negative-dim px-4 py-8 text-center">
                  <div className="mx-auto flex h-2 w-2 items-center justify-center">
                    <span className="h-2 w-2 rounded-full bg-negative shadow-[0_0_8px_0_var(--negative)]" />
                  </div>
                  <p className="mt-3 text-sm font-medium text-foreground">Couldn&rsquo;t load the ledger.</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {predictionErrorMessage || "The prediction service didn\u2019t respond. Try again in a moment."}
                  </p>
                </div>
              ) : (
                <>
                  <div className="space-y-3 p-4 lg:hidden">
                    {predsLoading
                      ? Array.from({ length: 4 }).map((_, index) => (
                          <div key={index} className="pred-card">
                            <Skeleton className="h-4 w-40" />
                            <div className="pred-card-grid">
                              <Skeleton className="h-10 w-full" />
                              <Skeleton className="h-10 w-full" />
                              <Skeleton className="h-10 w-full" />
                              <Skeleton className="h-10 w-full" />
                            </div>
                          </div>
                        ))
                      : filteredPredictions.length === 0
                        ? (
                          <div className="cosmos-table-empty">
                            {hasFilters
                              ? "No predictions matched the current filters."
                              : "No predictions matched the current view yet."}
                          </div>
                        )
                        : filteredPredictions.map((row) => (
                            <PredictionCard key={row.id} row={row} />
                          ))}
                  </div>

                  <div className="hidden lg:block">
                    <div className="cosmos-table-wrap">
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead className="w-32">Captured</TableHead>
                            <TableHead>Market / Subject</TableHead>
                            <TableHead className="w-20">Sport</TableHead>
                            <TableHead className="w-24">Side / {ENTRY_LABEL}</TableHead>
                            <TableHead className="w-20">Edge</TableHead>
                            <TableHead className="w-24">{WIN_PROB_LABEL}</TableHead>
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
                            : filteredPredictions.length === 0
                              ? (
                                <TableRow>
                                  <TableCell
                                    colSpan={9}
                                    className="cosmos-table-empty"
                                  >
                                    {hasFilters
                                      ? "No predictions matched the current filters."
                                      : "No predictions matched the current view yet."}
                                  </TableCell>
                                </TableRow>
                              )
                              : filteredPredictions.map((row) => (
                                  <PredictionRow key={row.id} row={row} />
                                ))}
                        </TableBody>
                      </Table>
                    </div>
                  </div>
                </>
              )}
            </div>
          </section>
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
