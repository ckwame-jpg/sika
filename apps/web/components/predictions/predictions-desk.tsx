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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Badge, SportBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty-state";
import { cn, fmtDatetime, fmtEdge, fmtPercent } from "@/lib/utils";
import { ENTRY_LABEL, RELIABILITY_LABEL, WIN_PROB_LABEL } from "@/lib/market-copy";
import { sportTint } from "@/lib/sport-tints";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import Link from "next/link";
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

function mean(values: number[]): number | null {
  if (values.length === 0) return null;
  let sum = 0;
  for (const v of values) sum += v;
  return sum / values.length;
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

function clampPct(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function rowLabel(row: PredictionRead): string {
  const displayTitle = row.display_market_title ?? row.market_title;
  return row.subject_name
    ? `${row.subject_name}${row.stat_key ? ` · ${row.stat_key}` : ""}${row.threshold != null ? ` ${row.threshold}` : ""}`
    : displayTitle;
}

/** Spec 5b gauge row: model hit rate / avg confidence / surfaced today. */
function GaugeRow({ summary, predictions }: { summary: PredictionSummaryRead; predictions: PredictionRead[] }) {
  const hitRate = summary.win_rate;
  const hitTone =
    hitRate == null ? "var(--gi-micro-rail)" : hitRate >= 0.5 ? "var(--gi-green)" : "var(--gi-orange)";

  const avgConf = summary.average_confidence;

  const today = new Date().toDateString();
  const surfacedToday = predictions.filter(
    (row) => new Date(row.captured_at).toDateString() === today,
  ).length;
  const surfacedPct = predictions.length > 0 ? (surfacedToday / predictions.length) * 100 : 0;

  return (
    <div className="gi-gauge-row three">
      <div className="gi-card gi-gauge-card" data-testid="pred-gauge-hit-rate">
        <div
          className="gi-gauge"
          style={{ "--gg-p": clampPct((hitRate ?? 0) * 100), "--gg-c": hitTone } as React.CSSProperties}
          aria-hidden
        >
          <span className="gi-gauge-value">{hitRate != null ? `${Math.round(hitRate * 100)}%` : "—"}</span>
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">model hit rate</span>
          <span className="gi-gauge-title">{fmtPercent(hitRate)}</span>
          <span className="gi-gauge-sub">
            {summary.settled_predictions} graded · {summary.won_predictions}w / {summary.lost_predictions}l
          </span>
        </div>
      </div>
      <div className="gi-card gi-gauge-card" data-testid="pred-gauge-confidence">
        <div
          className="gi-gauge"
          style={{ "--gg-p": clampPct((avgConf ?? 0) * 100), "--gg-c": "var(--color-cosmos-cyan-500)" } as React.CSSProperties}
          aria-hidden
        >
          <span className="gi-gauge-value">{avgConf != null ? (avgConf * 100).toFixed(0) : "—"}</span>
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">avg {RELIABILITY_LABEL}</span>
          <span className="gi-gauge-title">{fmtPercent(avgConf)}</span>
          <span className="gi-gauge-sub">
            {summary.average_edge != null ? `${fmtEdge(summary.average_edge)} avg edge` : "across shown picks"}
          </span>
        </div>
      </div>
      <div className="gi-card gi-gauge-card" data-testid="pred-gauge-surfaced">
        <div
          className="gi-gauge"
          style={{ "--gg-p": clampPct(surfacedPct), "--gg-c": "var(--color-cosmos-violet-500)" } as React.CSSProperties}
          aria-hidden
        >
          <span className="gi-gauge-value">{surfacedToday}</span>
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">surfaced today</span>
          <span className="gi-gauge-title">{surfacedToday} picks</span>
          <span className="gi-gauge-sub">of {summary.total_predictions} in view</span>
        </div>
      </div>
    </div>
  );
}

type BoardSort = "edge" | "prob" | "captured";

function BoardRow({ row, hero }: { row: PredictionRead; hero: boolean }) {
  const { formatPrice } = usePriceDisplay();
  const displayTitle = row.display_market_title ?? row.market_title;
  const winProbability = row.selected_side_probability ?? row.confidence;
  const label = rowLabel(row);
  const dimBar = row.edge >= 0 && row.edge < 0.04;
  const tint = row.sport_key ? sportTint(row.sport_key, "var(--color-cosmos-violet-default-tint)") : null;

  return (
    <div className={cn("gi-board-row", hero && "gi-hero-row")} data-testid="pred-board-row">
      <div className="min-w-0">
        <div className="gi-pick-title">
          {tint && <span className="dot" style={{ background: tint, width: 5, height: 5, borderRadius: 999, flex: "none", opacity: 0.8 }} aria-hidden />}
          <span className="t">{label}</span>
          {row.source_badge_label && <span className="gi-tag">{row.source_badge_label}</span>}
        </div>
        <div className="gi-pick-sub">
          {row.subject_name ? `${displayTitle} · ` : ""}
          {row.side.toLowerCase()} · captured {fmtDatetime(row.captured_at)}
        </div>
      </div>
      <div className="gi-board-hide-sm">
        <div className="gi-probbar-labels">
          <span>{WIN_PROB_LABEL}</span>
          <span className="val">{fmtPercent(winProbability)}</span>
        </div>
        <div
          className={cn("gi-probbar", hero && "hot", dimBar && "dim")}
          style={
            {
              "--pb-p": winProbability != null ? clampPct(winProbability * 100) : 0,
              "--pb-tick": row.suggested_price != null ? clampPct(row.suggested_price * 100) : 0,
            } as React.CSSProperties
          }
          aria-hidden
        >
          <span className="gi-probbar-fill" />
          {row.suggested_price != null && <span className="gi-probbar-tick" />}
        </div>
      </div>
      <span className="mono gi-board-hide-md">{formatPrice(row.suggested_price)}</span>
      <span className={cn("gi-edge", row.edge >= 0.08 ? "strong" : row.edge < 0 ? "neg" : row.edge < 0.04 ? "neutral" : "")}>
        {fmtEdge(row.edge)}
      </span>
      <span className="mono gi-board-hide-md">{Math.round(row.confidence * 100)}%</span>
      <span className="gi-board-hide-sm">
        <span className={cn("outcome-pill", settlementPillClass(row.settlement_status))}>
          {row.settlement_status}
        </span>
      </span>
      <span>
        <span className={cn("outcome-pill", outcomePillClass(row.prediction_outcome))}>
          {row.prediction_outcome}
        </span>
      </span>
    </div>
  );
}

function PredictionCard({ row }: { row: PredictionRead }) {
  const { formatPrice } = usePriceDisplay();
  const displayTitle = row.display_market_title ?? row.market_title;
  const winProbability = row.selected_side_probability ?? row.confidence;
  const label = rowLabel(row);
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

/** Edge-distribution rail: histogram over 0…+12%, median/top, top signal. */
function EdgeRail({ predictions }: { predictions: PredictionRead[] }) {
  const edges = predictions.map((row) => row.edge);
  const BUCKETS = 8;
  const MAX_EDGE = 0.12;
  const counts = new Array(BUCKETS).fill(0);
  for (const edge of edges) {
    const clamped = Math.max(0, Math.min(MAX_EDGE - 1e-9, edge));
    counts[Math.floor((clamped / MAX_EDGE) * BUCKETS)] += 1;
  }
  const peak = Math.max(...counts, 1);
  const med = median(edges);
  const top = edges.length > 0 ? Math.max(...edges) : null;

  const pending = predictions.filter((row) => row.settlement_status.toLowerCase() === "pending");
  const signalPool = pending.length > 0 ? pending : predictions;
  const topSignal =
    signalPool.length > 0
      ? signalPool.reduce((best, row) => (row.edge > best.edge ? row : best), signalPool[0])
      : null;
  const signalProb = topSignal ? (topSignal.selected_side_probability ?? topSignal.confidence) : null;

  return (
    <div className="gi-rail" data-testid="pred-edge-rail">
      <span className="gi-micro-label rail">edge distribution · {predictions.length} picks</span>
      <div>
        <div className="gi-histo" aria-hidden>
          {counts.map((count, index) => (
            <span
              key={index}
              className={cn("gi-histo-bar", count === peak && count > 0 && "peak")}
              style={{ "--hb-p": clampPct((count / peak) * 100) } as React.CSSProperties}
            />
          ))}
        </div>
        <div className="gi-histo-axis">
          <span>0%</span>
          <span>+4%</span>
          <span>+8%</span>
          <span>+12%</span>
        </div>
      </div>
      <div className="gi-rail-stat">
        <span>median edge</span>
        <span className="v" style={{ color: "var(--color-cosmos-violet-300)" }}>
          {med != null ? fmtEdge(med) : "—"}
        </span>
      </div>
      <div className="gi-rail-stat">
        <span>top edge</span>
        <span className="v" style={{ color: "var(--gi-green)" }}>{top != null ? fmtEdge(top) : "—"}</span>
      </div>
      {topSignal && (
        <>
          <div className="gi-rail-divider" />
          <span className="gi-micro-label rail">top signal</span>
          <div className="gi-stat-chip" style={{ flexDirection: "row", gap: 12, textAlign: "left", alignItems: "center" }}>
            <div
              className="gi-donut sm"
              style={{ "--gd-p": signalProb != null ? clampPct(signalProb * 100) : 0 } as React.CSSProperties}
              aria-hidden
            >
              <span className="gi-donut-ring" />
              <div className="gi-donut-center">
                <span className="gi-donut-value">
                  {signalProb != null ? `${Math.round(signalProb * 100)}%` : "—"}
                </span>
              </div>
            </div>
            <div className="min-w-0">
              <p className="truncate text-[12.5px] font-medium text-foreground">{rowLabel(topSignal)}</p>
              <p className="text-[10.5px] text-muted-foreground">
                strongest {pending.length > 0 ? "pending " : ""}pick · {fmtEdge(topSignal.edge)}
              </p>
            </div>
          </div>
        </>
      )}
      <Link href="/trade" className="gi-btn">
        open ticket
      </Link>
    </div>
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
  const [sort, setSort] = useState<BoardSort>("edge");

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
      // Bug #45 — settlement updates row state that other surfaces
      // read transitively: trade desk shows recommendation status,
      // positions shows realized PnL, /events shows event/result
      // status. Invalidate every prediction-derived surface so the
      // operator sees consistent state immediately, not on the next
      // background poll.
      await Promise.all([
        mutate((key) => typeof key === "string" && key.startsWith("/predictions")),
        mutate((key) => typeof key === "string" && key.startsWith("/predictions/summary")),
        mutate((key) => typeof key === "string" && key.startsWith("/parlays")),
        mutate((key) => typeof key === "string" && key.startsWith("/trade-desk")),
        mutate((key) => typeof key === "string" && key.startsWith("/positions")),
        mutate((key) => typeof key === "string" && key.startsWith("/events")),
        mutate((key) => typeof key === "string" && key.startsWith("/watchlist")),
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
  const sortedPredictions = [...filteredPredictions].sort((a, b) => {
    if (sort === "edge") return b.edge - a.edge;
    if (sort === "prob") {
      return (b.selected_side_probability ?? b.confidence) - (a.selected_side_probability ?? a.confidence);
    }
    return new Date(b.captured_at).getTime() - new Date(a.captured_at).getTime();
  });

  const boardDate = new Date().toLocaleDateString(undefined, { month: "short", day: "numeric" });

  return (
    <div className="gi-screen">
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
            <div className="gi-gauge-row three">
              {Array.from({ length: 3 }).map((_, index) => (
                <Skeleton key={index} className="h-24 w-full rounded-xl" />
              ))}
            </div>
          ) : summary ? (
            <GaugeRow summary={summary} predictions={predictions ?? []} />
          ) : null}

          <div className="gi-cols">
            <div className="gi-cols-main">
              <section className="gi-panel">
                <div className="gi-panel-head">
                  <span className="gi-glow-dot" aria-hidden />
                  <h2 className="gi-panel-title">model board — {boardDate}</h2>
                  <span className="ml-auto flex items-center gap-1.5">
                    <button
                      type="button"
                      className={cn("gi-chip", sort === "edge" && "active")}
                      onClick={() => setSort("edge")}
                    >
                      sort: edge
                    </button>
                    <button
                      type="button"
                      className={cn("gi-chip", sort === "prob" && "active")}
                      onClick={() => setSort("prob")}
                    >
                      prob
                    </button>
                    <button
                      type="button"
                      className={cn("gi-chip", sort === "captured" && "active")}
                      onClick={() => setSort("captured")}
                    >
                      captured
                    </button>
                  </span>
                </div>
                {predsError ? (
                  <EmptyState
                    tone="error"
                    title="Couldn&rsquo;t load the ledger."
                    description={
                      predictionErrorMessage ||
                      "The prediction service didn’t respond. Try again in a moment."
                    }
                  />
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
                        : sortedPredictions.length === 0
                          ? (
                            <div className="cosmos-table-empty">
                              {hasFilters
                                ? "No predictions matched the current filters."
                                : "No predictions matched the current view yet."}
                            </div>
                          )
                          : sortedPredictions.map((row) => (
                              <PredictionCard key={row.id} row={row} />
                            ))}
                    </div>

                    <div className="hidden lg:block">
                      <div className="gi-board-colhead">
                        <span>market</span>
                        <span className="gi-board-hide-sm">model vs market</span>
                        <span className="gi-board-hide-md">{ENTRY_LABEL}</span>
                        <span>edge</span>
                        <span className="gi-board-hide-md">conf</span>
                        <span className="gi-board-hide-sm">settlement</span>
                        <span>outcome</span>
                      </div>
                      <div className="gi-board-rows">
                        {predsLoading
                          ? Array.from({ length: 8 }).map((_, index) => (
                              <div key={index} className="gi-board-row">
                                <Skeleton className="h-9 w-full" />
                              </div>
                            ))
                          : sortedPredictions.length === 0
                            ? (
                              <div className="cosmos-table-empty px-[18px] py-8">
                                {hasFilters
                                  ? "No predictions matched the current filters."
                                  : "No predictions matched the current view yet."}
                              </div>
                            )
                            : sortedPredictions.map((row, index) => (
                                <BoardRow key={row.id} row={row} hero={sort === "edge" && index === 0} />
                              ))}
                      </div>
                      {sortedPredictions.length > 0 && (
                        <div className="gi-panel-foot">
                          showing {sortedPredictions.length} of {predictions?.length ?? 0} captured picks
                        </div>
                      )}
                    </div>
                  </>
                )}
              </section>
            </div>
            <div className="gi-cols-rail hidden xl:block">
              <EdgeRail predictions={sortedPredictions} />
            </div>
          </div>
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
