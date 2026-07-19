"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { RefreshCw, X } from "lucide-react";
import { fetchTradeDesk, keys } from "@/lib/api";
import type {
  TradeDeskArchivedSlate,
  TradeDeskEvent,
  TradeDeskGameLine,
  TradeDeskResponse,
  TradeDeskThreshold,
} from "@/lib/types";
import { cn, fmtDatetime, fmtEdge, fmtPercent, fmtPrice, fmtRelative, fmtStartsAt, sportLabel } from "@/lib/utils";
import { PaperParlayDialog } from "@/components/parlays/paper-parlay-dialog";
import { ParlayTray } from "@/components/parlays/parlay-tray";
import { TradeSelection, TradeTicket } from "@/components/trade/trade-ticket";

interface TradeKpis {
  events: number;
  live: number;
  upcoming: number;
  candidateMarkets: number;
  recommendations: number;
  avgEdge: number | null;
  topQuartileEdge: number | null;
}

import { sportTint as sharedSportTint } from "@/lib/sport-tints";

// Bug #30 — keep the trade-desk-specific fallback color while sharing
// the SPORT_TINTS map. The fallback is only ever consumed as a CSS custom
// property value, so var() is safe here; it resolves to the same violet
// the literal used to spell out.
function sportTint(sport: string): string {
  return sharedSportTint(sport, "var(--color-cosmos-violet-default-tint)");
}

/**
 * Short badge text for a game-line market_kind. Multiple kinds can
 * share the same ``display_label`` (e.g. both the full-game winner
 * and the first-five-innings winner read "Kansas City Royals to
 * win") — surface the kind here so the operator can disambiguate at
 * a glance. Returns ``null`` for kinds where the row stands alone
 * (spreads/totals already encode their variant in the line).
 */
function formatMarketKindBadge(marketKind: string): string | null {
  switch (marketKind) {
    case "first_five_winner":
      return "F5";
    case "game_winner":
    case "moneyline":
      return "FG";
    default:
      return null;
  }
}

/** Flat pool of every scored edge in the slate. */
function collectAllEdges(events: TradeDeskEvent[]): number[] {
  const edges: number[] = [];
  for (const event of events) {
    for (const line of event.game_lines) edges.push(line.edge);
    for (const player of event.player_props) {
      for (const group of player.stat_groups) {
        for (const threshold of group.thresholds) edges.push(threshold.edge);
      }
    }
  }
  return edges;
}

function mean(values: number[]): number | null {
  if (values.length === 0) return null;
  let sum = 0;
  for (const v of values) sum += v;
  return sum / values.length;
}

function selectionExistsInEvents(events: TradeDeskEvent[], ticker: string): boolean {
  return events.some((event) =>
    event.game_lines.some((line) => line.ticker === ticker) ||
    event.player_props.some((player) =>
      player.stat_groups.some((group) => group.thresholds.some((threshold) => threshold.ticker === ticker))
    )
  );
}

/**
 * Nearest-rank quantile (NIST method C1).
 * For q=0.75, n=7: index = ceil(0.75 * 7) - 1 = 5.
 * Deterministic; matches Phase 1 test fixture expectation "+10.0%".
 */
function quantileNearestRank(values: number[], q: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const rank = Math.ceil(q * sorted.length) - 1;
  const idx = Math.max(0, Math.min(sorted.length - 1, rank));
  return sorted[idx];
}

/** "4h 29m" / "29m" close countdown for pick sub-lines. */
function fmtClose(minutes: number | null | undefined): string | null {
  if (minutes == null || !Number.isFinite(minutes)) return null;
  if (minutes <= 0) return "closing";
  const rounded = Math.round(minutes);
  const h = Math.floor(rounded / 60);
  const m = rounded % 60;
  if (h === 0) return `closes ${m}m`;
  return `closes ${h}h ${String(m).padStart(2, "0")}m`;
}

function SlateStatusPill({ data }: { data: TradeDeskResponse }) {
  if (data.freshness_status === "fresh") return null;
  const generatedAt = data.generated_at ?? null;
  const relative = generatedAt ? fmtRelative(generatedAt) : null;
  const message =
    data.freshness_status === "stale"
      ? `Showing last known slate${relative ? ` (snapshot ${relative})` : ""}. Refresh is behind.`
      : data.blocking_reason ||
        (data.freshness_status === "empty"
          ? "Current slate scored successfully, but no markets cleared recommendation thresholds."
          : "Current slate refresh did not produce a usable trade desk.");
  return (
    <div className="slate-status-pill" role="status" data-testid="trade-desk-status-pill">
      <span className="font-medium">{message}</span>
    </div>
  );
}

function SlateHealthDetails({ data }: { data: TradeDeskResponse }) {
  return (
    <div className="mt-4 grid gap-2 text-left sm:grid-cols-2 lg:grid-cols-4">
      {[
        ["Current events", data.event_count],
        ["Candidate markets", data.candidate_market_count],
        ["Scored markets", data.scored_market_count],
        ["Recommendations", data.recommendation_count],
      ].map(([label, value]) => (
        <div
          key={label}
          className="rounded-xl border border-border bg-background/40 px-3 py-2"
        >
          <p className="text-2xs uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
          <p className="mt-1 font-mono text-base text-foreground">{value}</p>
        </div>
      ))}
    </div>
  );
}

function computeTradeKpis(data: TradeDeskResponse): TradeKpis {
  const events = data.events;
  const live = events.filter((event) => event.event_status === "in_progress").length;
  const upcoming = events.filter((event) => event.event_status === "scheduled").length;
  const allEdges = collectAllEdges(events);
  return {
    events: events.length,
    live,
    upcoming,
    candidateMarkets: data.candidate_market_count,
    recommendations: data.recommendation_count,
    avgEdge: mean(allEdges),
    topQuartileEdge: quantileNearestRank(allEdges, 0.75),
  };
}

/** Minutes until the next market close across the whole slate. */
function nextCloseMinutes(events: TradeDeskEvent[]): number | null {
  let min: number | null = null;
  const consider = (value: number | null | undefined) => {
    if (value == null || value <= 0) return;
    if (min == null || value < min) min = value;
  };
  for (const event of events) {
    for (const line of event.game_lines) consider(line.time_to_close_minutes);
    for (const player of event.player_props) {
      for (const group of player.stat_groups) {
        for (const threshold of group.thresholds) consider(threshold.time_to_close_minutes);
      }
    }
  }
  return min;
}

function clampPct(value: number): number {
  return Math.max(0, Math.min(100, value));
}

interface GaugeCardProps {
  micro: string;
  title: string;
  sub: string;
  gauge:
    | { kind: "ring"; pct: number; value: string; color: string }
    | { kind: "orb" };
  titleClassName?: string;
  testId: string;
}

function GaugeCard({ micro, title, sub, gauge, titleClassName, testId }: GaugeCardProps) {
  return (
    <div className="gi-card gi-gauge-card" data-testid={testId}>
      {gauge.kind === "ring" ? (
        <div
          className="gi-gauge"
          style={{ "--gg-p": clampPct(gauge.pct), "--gg-c": gauge.color } as React.CSSProperties}
          aria-hidden
        >
          <span className="gi-gauge-value">{gauge.value}</span>
        </div>
      ) : (
        <div className="gi-orb-stat" aria-hidden>
          <span className="core" />
        </div>
      )}
      <div className="gi-gauge-meta">
        <span className="gi-micro-label">{micro}</span>
        <span className={cn("gi-gauge-title", titleClassName)}>{title}</span>
        <span className="gi-gauge-sub">{sub}</span>
      </div>
    </div>
  );
}

/** Spec 5a gauge row: slate health / avg edge / top quartile / events orb. */
function GaugeRow({ data, kpis }: { data: TradeDeskResponse; kpis: TradeKpis }) {
  const scored = data.scored_market_count;
  const candidates = data.candidate_market_count;
  const healthPct = candidates > 0 ? Math.round((scored / candidates) * 100) : 0;
  const fresh = data.freshness_status;
  const healthColor =
    fresh === "fresh" ? "var(--gi-green)" : fresh === "empty" ? "var(--gi-micro-rail)" : "var(--gi-amber)";

  const nextClose = fmtClose(nextCloseMinutes(data.events));

  return (
    <div className="gi-gauge-row">
      <GaugeCard
        testId="trade-gauge-health"
        micro="slate health"
        title={fresh}
        sub={`${scored} of ${candidates} scored`}
        gauge={{ kind: "ring", pct: healthPct, value: `${healthPct}%`, color: healthColor }}
      />
      <GaugeCard
        testId="trade-gauge-avg-edge"
        micro="avg edge"
        title={kpis.avgEdge != null ? fmtEdge(kpis.avgEdge) : "—"}
        sub="gauge vs +10% cap"
        gauge={{
          kind: "ring",
          pct: kpis.avgEdge != null ? clampPct((kpis.avgEdge / 0.1) * 100) : 0,
          value: kpis.avgEdge != null ? (kpis.avgEdge * 100).toFixed(1) : "—",
          color: "var(--color-cosmos-violet-500)",
        }}
      />
      <GaugeCard
        testId="trade-gauge-top-quartile"
        micro="top quartile"
        title={kpis.topQuartileEdge != null ? fmtEdge(kpis.topQuartileEdge) : "—"}
        sub={`${kpis.recommendations} picks past bar`}
        gauge={{
          kind: "ring",
          pct: kpis.topQuartileEdge != null ? clampPct((kpis.topQuartileEdge / 0.1) * 100) : 0,
          value: kpis.topQuartileEdge != null ? (kpis.topQuartileEdge * 100).toFixed(1) : "—",
          color: "var(--color-cosmos-cyan-500)",
        }}
      />
      <GaugeCard
        testId="trade-gauge-events"
        micro="events tracked"
        title={`${kpis.events} · ${kpis.live} live`}
        sub={nextClose ? `next ${nextClose.replace(/^closes /, "close ")}` : `${kpis.upcoming} upcoming`}
        gauge={{ kind: "orb" }}
      />
    </div>
  );
}

function buildGameLineSelection(event: TradeDeskEvent, line: TradeDeskGameLine): TradeSelection {
  return {
    kind: "game_line",
    ticker: line.ticker,
    eventId: event.event_id,
    marketTitle: line.market_title,
    eventName: event.event_name,
    sportKey: event.sport_key,
    marketKind: line.market_kind,
    displayLabel: line.display_label,
    projectedSideLabel: line.projected_side_label,
    selectedSide: line.selected_side,
    selectedSideProbability: line.selected_side_probability,
    entryPrice: line.entry_price,
    edge: line.edge,
    confidence: line.confidence,
    kalshiUrl: line.kalshi_url,
    numericLine: line.numeric_line,
    totalDirection: line.total_direction,
    freshnessStaleGroups: line.freshness_stale_groups,
    freshnessConfidenceDelta: line.freshness_confidence_delta,
  };
}

function buildPlayerPropSelection(
  event: TradeDeskEvent,
  subjectName: string,
  subjectTeam: string | null,
  statKey: string,
  threshold: TradeDeskThreshold,
): TradeSelection {
  return {
    kind: "player_prop",
    ticker: threshold.ticker,
    eventId: event.event_id,
    marketTitle: `${subjectName}: ${threshold.threshold}+ ${statKey.replace(/_/g, " ")}`,
    eventName: event.event_name,
    sportKey: event.sport_key,
    marketKind: "player_prop",
    displayLabel: `${subjectName} ${threshold.threshold}+ ${statKey.replace(/_/g, " ")}`,
    projectedSideLabel: null,
    selectedSide: threshold.selected_side,
    selectedSideProbability: threshold.selected_side_probability ?? threshold.probability_yes,
    entryPrice: threshold.entry_price,
    edge: threshold.edge,
    confidence: threshold.confidence,
    kalshiUrl: threshold.kalshi_url,
    subjectName,
    subjectTeam,
    statKey,
    threshold: threshold.threshold,
    predictionInterval: threshold.prediction_interval,
    freshnessStaleGroups: threshold.freshness_stale_groups,
    freshnessConfidenceDelta: threshold.freshness_confidence_delta,
  };
}

/** One flattened, edge-sorted pick (game line or prop threshold). */
interface PickRowData {
  selection: TradeSelection;
  kindTag: string | null;
  closeMinutes: number | null;
}

function flattenEventPicks(event: TradeDeskEvent): PickRowData[] {
  const rows: PickRowData[] = [];
  for (const line of event.game_lines) {
    rows.push({
      selection: buildGameLineSelection(event, line),
      kindTag: formatMarketKindBadge(line.market_kind),
      closeMinutes: line.time_to_close_minutes,
    });
  }
  for (const player of event.player_props) {
    for (const group of player.stat_groups) {
      for (const threshold of group.thresholds) {
        rows.push({
          selection: buildPlayerPropSelection(
            event,
            player.subject_name,
            player.subject_team,
            group.stat_key,
            threshold,
          ),
          kindTag: null,
          closeMinutes: threshold.time_to_close_minutes,
        });
      }
    }
  }
  return rows.sort((a, b) => b.selection.edge - a.selection.edge);
}

// Spec 5a: the trade desk opens with the featured game already expanded
// and its hero pick loaded in the ticket rail — not a wall of collapsed
// strips over empty starfield. Featured = the event holding the slate's
// top-edge pick (flattenEventPicks sorts desc, so picks[0] is each
// event's best; the global max lives in exactly one event).
function findFeaturedPick(
  events: TradeDeskEvent[],
): { eventId: number; hero: TradeSelection } | null {
  let best: { eventId: number; hero: TradeSelection } | null = null;
  for (const event of events) {
    const top = flattenEventPicks(event)[0];
    if (!top) continue;
    if (!best || top.selection.edge > best.hero.edge) {
      best = { eventId: event.event_id, hero: top.selection };
    }
  }
  return best;
}

function countEventPicks(event: TradeDeskEvent): number {
  return (
    event.game_lines.length +
    event.player_props.reduce(
      (total, player) =>
        total + player.stat_groups.reduce((groupTotal, group) => groupTotal + group.thresholds.length, 0),
      0,
    )
  );
}

function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

function edgeToneClass(edge: number): string {
  if (edge < 0) return "neg";
  if (edge >= 0.08) return "strong";
  if (edge < 0.04) return "neutral";
  return "";
}

function PickRowButton({
  row,
  hero,
  isSelected,
  onSelect,
}: {
  row: PickRowData;
  hero: boolean;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const { selection } = row;
  const prob = selection.selectedSideProbability;
  const price = selection.entryPrice;
  const closeText = fmtClose(row.closeMinutes);
  const dimBar = selection.edge >= 0 && selection.edge < 0.04;
  return (
    <button
      type="button"
      onClick={onSelect}
      data-testid="trade-pick-row"
      className={cn("gi-pick-row focus-visible:ring-focus", hero && !isSelected && "gi-hero-row", isSelected && "selected")}
    >
      <div className="min-w-0">
        <div className="gi-pick-title">
          <span className="t">{selection.displayLabel}</span>
          {row.kindTag && (
            <span className="gi-tag" data-testid="line-row-kind-badge">
              {row.kindTag}
            </span>
          )}
        </div>
        <div className="gi-pick-sub">
          {selection.selectedSide} {fmtPrice(price)}
          {closeText ? ` · ${closeText}` : ""}
        </div>
      </div>
      <div className="gi-pick-probcol">
        <div className="gi-probbar-labels">
          <span>win prob</span>
          <span className="val">{fmtPercent(prob)}</span>
        </div>
        <div
          className={cn("gi-probbar", hero && "hot", dimBar && "dim")}
          style={
            {
              "--pb-p": prob != null ? clampPct(prob * 100) : 0,
              "--pb-tick": price != null ? clampPct(price * 100) : 0,
            } as React.CSSProperties
          }
          aria-hidden
        >
          <span className="gi-probbar-fill" />
          {price != null && <span className="gi-probbar-tick" />}
        </div>
      </div>
      <div className="gi-pick-edge-col">
        <span className={cn("gi-edge", edgeToneClass(selection.edge))}>{fmtEdge(selection.edge)}</span>
      </div>
    </button>
  );
}

interface TradeEventListProps {
  events: TradeDeskEvent[];
  expandedEventIds: Set<number>;
  idPrefix: string;
  selected: TradeSelection | null;
  onToggleEvent: (eventId: number) => void;
  onSelect: (selection: TradeSelection) => void;
}

function TradeEventList({
  events,
  expandedEventIds,
  idPrefix,
  selected,
  onToggleEvent,
  onSelect,
}: TradeEventListProps) {
  return (
    <>
      {events.map((event) => {
        const candidateCount = event.candidate_market_count ?? 0;
        const scoredCount = event.scored_market_count ?? 0;
        const coverageCount = event.coverage_prediction_count ?? 0;
        const picks = flattenEventPicks(event);
        const showCoverageOnly = picks.length === 0 && (candidateCount > 0 || scoredCount > 0 || coverageCount > 0);
        if (picks.length === 0 && !showCoverageOnly) {
          return null;
        }

        const isExpanded = expandedEventIds.has(event.event_id);
        const marketsId = `${idPrefix}-trade-event-${event.event_id}-markets`;
        const live = event.event_status === "in_progress";
        const tint = sportTint(event.sport_key);
        const stripSummary = [
          pluralize(picks.length, "pick"),
          coverageCount > 0 && picks.length === 0 ? `${coverageCount} coverage` : null,
        ]
          .filter(Boolean)
          .join(" · ");

        if (!isExpanded) {
          return (
            <button
              key={`${idPrefix}-${event.event_id}`}
              type="button"
              className="gi-game-strip focus-visible:ring-focus"
              aria-expanded={false}
              aria-controls={marketsId}
              onClick={() => onToggleEvent(event.event_id)}
              data-testid="trade-event-toggle"
              style={{ "--gd": tint } as React.CSSProperties}
            >
              <span className="gi-glow-dot" aria-hidden />
              <span className="name">{event.event_name}</span>
              {live ? (
                <span className="gi-live-chip">live</span>
              ) : (
                <span className="when">{fmtStartsAt(event.starts_at)}</span>
              )}
              <span className="count">{stripSummary} ›</span>
            </button>
          );
        }

        return (
          <section
            key={`${idPrefix}-${event.event_id}`}
            className="gi-panel"
            style={{ "--gd": tint } as React.CSSProperties}
          >
            <button
              type="button"
              className="gi-panel-head w-full text-left focus-visible:ring-focus"
              aria-expanded
              aria-controls={marketsId}
              onClick={() => onToggleEvent(event.event_id)}
              data-testid="trade-event-toggle"
            >
              <span className="gi-glow-dot" aria-hidden />
              <h2 className="gi-panel-title">{event.event_name}</h2>
              <span className="gi-panel-sub">
                {sportLabel(event.sport_key)} · {fmtStartsAt(event.starts_at)}
              </span>
              {live && <span className="gi-live-chip">live</span>}
              <span className="gi-count-chip">{pluralize(picks.length, "pick")}</span>
            </button>

            <div id={marketsId} className="gi-pick-rows">
              {picks.map((row, index) => (
                <PickRowButton
                  key={row.selection.ticker}
                  row={row}
                  hero={index === 0 && picks.length > 0}
                  isSelected={selected?.ticker === row.selection.ticker}
                  onSelect={() => onSelect(row.selection)}
                />
              ))}
              {showCoverageOnly && (
                <div className="px-[18px] py-4">
                  <p className="gi-micro-label">Coverage</p>
                  <p className="mt-2 text-sm text-muted-foreground">
                    No bet cleared bet filters for this event.
                    {coverageCount > 0 ? ` ${coverageCount} coverage predictions were captured.` : ""}
                  </p>
                </div>
              )}
            </div>
            {showCoverageOnly && (
              <div className="gi-panel-foot">{pluralize(scoredCount, "scored market")}</div>
            )}
          </section>
        );
      })}
    </>
  );
}

function ArchivedSlateSection({
  slate,
  expanded,
  expandedEventIds,
  selected,
  onToggle,
  onToggleEvent,
  onSelect,
}: {
  slate: TradeDeskArchivedSlate;
  expanded: boolean;
  expandedEventIds: Set<number>;
  selected: TradeSelection | null;
  onToggle: () => void;
  onToggleEvent: (eventId: number) => void;
  onSelect: (selection: TradeSelection) => void;
}) {
  const panelId = "trade-previous-slate";
  return (
    <section className="gi-panel" data-testid="trade-previous-slate">
      <button
        type="button"
        className="gi-panel-head w-full text-left focus-visible:ring-focus"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={onToggle}
      >
        <span className="gi-glow-dot" style={{ "--gd": "var(--gi-amber)" } as React.CSSProperties} aria-hidden />
        <h2 className="gi-panel-title">Last good slate · {fmtDatetime(slate.generated_at)}</h2>
        <span className="gi-count-chip">
          {slate.recommendation_count} picks · {slate.scored_market_count} scored
        </span>
      </button>
      <div id={panelId} hidden={!expanded}>
        <div className="flex flex-wrap gap-x-4 gap-y-1 px-[18px] py-3 text-[11px] text-muted-foreground">
          <span>{slate.event_count} events</span>
          <span>{slate.candidate_market_count} candidates</span>
          <span>{slate.coverage_prediction_count} coverage</span>
          {slate.generated_from_run_id ? <span>run #{slate.generated_from_run_id}</span> : null}
        </div>
        <div className="flex flex-col gap-3 px-3 pb-3">
          <TradeEventList
            events={slate.events}
            expandedEventIds={expandedEventIds}
            idPrefix="archived"
            selected={selected}
            onToggleEvent={onToggleEvent}
            onSelect={onSelect}
          />
        </div>
      </div>
    </section>
  );
}

// ---- Slate instruments -------------------------------------------------
// The 5a mock was drawn against a dense slate; real slates are often two
// or three strips, which left the lower half of the desk as bare
// starfield. These panels are the designed fill: they read the slate the
// operator already loaded (no extra fetches) and turn the void into
// instrument surface — the shape of the board, and what closes next.

function pickMedian(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

function SlateInstruments({
  picks,
  onSelect,
  selectedTicker,
}: {
  picks: PickRowData[];
  onSelect: (selection: TradeSelection) => void;
  selectedTicker: string | null;
}) {
  if (picks.length === 0) return null;

  const edges = picks.map((row) => row.selection.edge);
  const BUCKETS = 8;
  const MAX_EDGE = 0.12;
  const counts = new Array<number>(BUCKETS).fill(0);
  for (const edge of edges) {
    const clamped = Math.max(0, Math.min(MAX_EDGE - 1e-9, edge));
    counts[Math.floor((clamped / MAX_EDGE) * BUCKETS)] += 1;
  }
  const peak = Math.max(...counts, 1);
  const med = pickMedian(edges);
  const top = Math.max(...edges);

  const closing = picks
    .filter((row) => row.closeMinutes != null)
    .sort((a, b) => a.closeMinutes! - b.closeMinutes!)
    .slice(0, 5);

  return (
    <div className="grid gap-4 xl:grid-cols-2" data-testid="trade-slate-instruments">
      <section className="gi-panel" data-testid="trade-edge-histogram">
        <div className="gi-panel-head">
          <span className="gi-glow-dot" aria-hidden />
          <h2 className="gi-panel-title">edge distribution</h2>
          <span className="gi-count-chip">{pluralize(picks.length, "pick")}</span>
        </div>
        <div className="px-[18px] pb-4 pt-4">
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
          <div className="mt-4 grid grid-cols-2 gap-3">
            <div className="gi-stat-chip">
              <span className="k">median edge</span>
              <span className="v" style={{ color: "var(--color-cosmos-violet-300)" }}>
                {med != null ? fmtEdge(med) : "—"}
              </span>
            </div>
            <div className="gi-stat-chip">
              <span className="k">top edge</span>
              <span className="v pos">{fmtEdge(top)}</span>
            </div>
          </div>
        </div>
      </section>

      {closing.length > 0 && (
        <section className="gi-panel" data-testid="trade-closing-next">
          <div className="gi-panel-head">
            <span
              className="gi-glow-dot"
              style={{ "--gd": "var(--color-cosmos-violet-500)" } as React.CSSProperties}
              aria-hidden
            />
            <h2 className="gi-panel-title">closing next</h2>
            <span className="gi-count-chip">{pluralize(closing.length, "market")}</span>
          </div>
          <div>
            {closing.map((row) => {
              const closeText = fmtClose(row.closeMinutes);
              return (
                <button
                  key={row.selection.ticker}
                  type="button"
                  onClick={() => onSelect(row.selection)}
                  data-testid="trade-closing-row"
                  className={cn(
                    "flex w-full items-center gap-4 border-t border-white/5 px-[18px] py-3 text-left transition first:border-t-0 hover:bg-white/[.03] focus-visible:ring-focus",
                    selectedTicker === row.selection.ticker && "bg-white/[.04]",
                  )}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-medium text-foreground">
                      {row.selection.displayLabel}
                    </span>
                    <span className="block truncate text-[11px] text-muted-foreground">
                      {row.selection.eventName}
                    </span>
                  </span>
                  <span className="shrink-0 font-mono text-[11px] text-muted-foreground">{closeText}</span>
                  <span className={cn("gi-edge shrink-0 text-[13px]", edgeToneClass(row.selection.edge))}>
                    {fmtEdge(row.selection.edge)}
                  </span>
                </button>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}

// Rail queue under the ticket: the next-strongest picks one click away,
// so the 320px rail column carries more than a lone ticket card.
function NextUpQueue({
  picks,
  excludeTicker,
  onSelect,
}: {
  picks: PickRowData[];
  excludeTicker: string | null;
  onSelect: (selection: TradeSelection) => void;
}) {
  const queue = picks
    .filter((row) => row.selection.ticker !== excludeTicker)
    .sort((a, b) => b.selection.edge - a.selection.edge)
    .slice(0, 3);
  if (queue.length === 0) return null;

  return (
    <div className="gi-card mt-4" data-testid="trade-next-up">
      <p className="gi-micro-label">next up</p>
      <div className="mt-2">
        {queue.map((row) => (
          <button
            key={row.selection.ticker}
            type="button"
            onClick={() => onSelect(row.selection)}
            data-testid="trade-next-up-row"
            className="flex w-full items-center gap-3 border-t border-white/5 py-2.5 text-left transition first:border-t-0 hover:bg-white/[.03] focus-visible:ring-focus"
          >
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[12.5px] font-medium text-foreground">
                {row.selection.displayLabel}
              </span>
              <span className="block truncate text-[10.5px] text-muted-foreground">
                {row.selection.eventName}
              </span>
            </span>
            <span className={cn("gi-edge shrink-0 text-[13px]", edgeToneClass(row.selection.edge))}>
              {fmtEdge(row.selection.edge)}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

export function TradeDesk({ sport }: { sport?: string }) {
  const [selected, setSelected] = useState<TradeSelection | null>(null);
  const [expandedEventIds, setExpandedEventIds] = useState<Set<number>>(() => new Set());
  const [archivedExpandedEventIds, setArchivedExpandedEventIds] = useState<Set<number>>(() => new Set());
  const [archiveState, setArchiveState] = useState<{ key: string | null; expanded: boolean } | null>(null);
  const [parlayDialogOpen, setParlayDialogOpen] = useState(false);
  const { data, error, isLoading, mutate } = useSWR<TradeDeskResponse>(
    keys.tradeDesk(sport),
    () => fetchTradeDesk(sport),
    { refreshInterval: 30_000 },
  );

  useEffect(() => {
    if (!selected || !data) {
      return;
    }

    const selectionStillVisible =
      selectionExistsInEvents(data.events, selected.ticker) ||
      selectionExistsInEvents(data.previous_slate?.events ?? [], selected.ticker);

    if (!selectionStillVisible) {
      setSelected(null);
    }
  }, [data, selected]);

  useEffect(() => {
    if (!data) return;
    setExpandedEventIds((current) => {
      const visibleEventIds = new Set(data.events.map((event) => event.event_id));
      const next = new Set<number>();
      for (const eventId of current) {
        if (visibleEventIds.has(eventId)) {
          next.add(eventId);
        }
      }
      return next.size === current.size ? current : next;
    });
    setArchivedExpandedEventIds((current) => {
      const visibleEventIds = new Set((data.previous_slate?.events ?? []).map((event) => event.event_id));
      const next = new Set<number>();
      for (const eventId of current) {
        if (visibleEventIds.has(eventId)) {
          next.add(eventId);
        }
      }
      return next.size === current.size ? current : next;
    });
  }, [data]);

  // Auto-expand the featured game once per sport view. The ref (not
  // state) records that we've initialized, so the 30s SWR refresh can't
  // re-open a panel the user deliberately collapsed. It re-arms on
  // sport switch, and stays unarmed while the slate has no picks — if
  // picks land on a later refresh, the desk fills itself in.
  const featured = useMemo(() => findFeaturedPick(data?.events ?? []), [data]);
  const allPicks = useMemo(() => (data?.events ?? []).flatMap(flattenEventPicks), [data]);
  const autoExpandedFor = useRef<string | null>(null);
  useEffect(() => {
    const viewKey = sport ?? "all";
    if (!featured || autoExpandedFor.current === viewKey) return;
    autoExpandedFor.current = viewKey;
    setExpandedEventIds((current) => {
      if (current.has(featured.eventId)) return current;
      const next = new Set(current);
      next.add(featured.eventId);
      return next;
    });
  }, [featured, sport]);

  function toggleEvent(eventId: number) {
    setExpandedEventIds((current) => {
      const next = new Set(current);
      if (next.has(eventId)) {
        next.delete(eventId);
      } else {
        next.add(eventId);
      }
      return next;
    });
  }

  function toggleArchivedEvent(eventId: number) {
    setArchivedExpandedEventIds((current) => {
      const next = new Set(current);
      if (next.has(eventId)) {
        next.delete(eventId);
      } else {
        next.add(eventId);
      }
      return next;
    });
  }

  function selectOrToggle(selection: TradeSelection) {
    setSelected((current) => (current?.ticker === selection.ticker ? null : selection));
  }

  if (isLoading && !data) {
    return (
      <div className="flex items-center justify-center py-12">
        <RefreshCw size={16} className="animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error) {
    // The 30s ``refreshInterval`` will eventually self-heal, but the
    // operator shouldn't have to wait — give them a manual retry so a
    // transient timeout (the most common case here) is a one-click fix.
    return (
      <div
        className="rounded-2xl border border-negative/30 bg-negative/10 px-4 py-6 text-center"
        data-testid="trade-desk-error"
      >
        <p className="text-sm font-medium text-foreground">Trade desk failed to load.</p>
        <p className="mt-1 text-sm text-muted-foreground">{error.message}</p>
        <button
          type="button"
          onClick={() => void mutate()}
          data-testid="trade-desk-retry"
          className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-hover/40 px-3 py-1 text-xs font-medium text-foreground transition hover:bg-surface-hover focus-visible:ring-focus"
        >
          <RefreshCw size={12} />
          Retry
        </button>
      </div>
    );
  }

  if (!data) {
    return null;
  }

  const kpis = computeTradeKpis(data);
  const previousSlate = data.previous_slate;
  const archiveKey = previousSlate?.generated_at ?? null;
  const archiveDefaultExpanded = Boolean(
    previousSlate && (data.events.length === 0 || data.freshness_status === "degraded" || data.freshness_status === "empty"),
  );
  const archiveExpanded = previousSlate
    ? archiveState?.key === archiveKey
      ? archiveState.expanded
      : archiveDefaultExpanded
    : false;

  function toggleArchive() {
    setArchiveState({ key: archiveKey, expanded: !archiveExpanded });
  }

  return (
    <div className="gi-screen">
      <SlateStatusPill data={data} />
      <GaugeRow data={data} kpis={kpis} />

      <div className="gi-cols">
        <div className="gi-cols-main">
          <TradeEventList
            events={data.events}
            expandedEventIds={expandedEventIds}
            idPrefix="current"
            selected={selected}
            onToggleEvent={toggleEvent}
            onSelect={selectOrToggle}
          />

          {data.events.length === 0 && (
            <div className="gi-panel px-4 py-8 text-center">
              <p className="text-sm font-medium text-foreground">
                {data.freshness_status === "empty"
                  ? `No markets cleared thresholds${sport ? ` for ${sportLabel(sport)}` : ""}.`
                  : data.freshness_status === "degraded"
                    ? `Current slate is degraded${sport ? ` for ${sportLabel(sport)}` : ""}.`
                    : `No live trade-ready markets${sport ? ` for ${sportLabel(sport)}` : ""}.`}
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
                {data.blocking_reason || "Markets appear here when the current slate is populated."}
              </p>
              {(data.freshness_status === "degraded" || data.freshness_status === "empty") && (
                <SlateHealthDetails data={data} />
              )}
            </div>
          )}

          <SlateInstruments
            picks={allPicks}
            onSelect={selectOrToggle}
            selectedTicker={selected?.ticker ?? null}
          />

          {previousSlate ? (
            <ArchivedSlateSection
              slate={previousSlate}
              expanded={archiveExpanded}
              expandedEventIds={archivedExpandedEventIds}
              selected={selected}
              onToggle={toggleArchive}
              onToggleEvent={toggleArchivedEvent}
              onSelect={selectOrToggle}
            />
          ) : null}

          {data.research_sports.length > 0 && (
            <div className="space-y-2">
              <p className="gi-micro-label">Research Only</p>
              <div className="grid gap-3 md:grid-cols-3">
                {data.research_sports.map((sportRow) => (
                  <div key={sportRow.sport_key} className="gi-card">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-sm font-medium text-foreground">{sportLabel(sportRow.sport_key)}</p>
                      <span className="gi-tag">Research</span>
                    </div>
                    <div className="mt-3 space-y-1 text-xs text-muted-foreground">
                      <p>{sportRow.events_count} tracked events</p>
                      <p>{sportRow.recommendations_count} current recommendations</p>
                      <p>Last refresh {sportRow.last_refresh_at ? fmtStartsAt(sportRow.last_refresh_at) : "—"}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Desktop rail falls back to the hero pick so it never opens on
            the "pick a market." empty state (spec 5a preloads the ticket).
            The mobile sheet below intentionally keeps `selected` — a
            fallback there would slide the drawer open on page load. */}
        <div className="gi-cols-rail trade-ticket-rail hidden lg:block" data-testid="trade-ticket-rail">
          <TradeTicket selection={selected ?? featured?.hero ?? null} />
          <NextUpQueue
            picks={allPicks}
            excludeTicker={(selected ?? featured?.hero)?.ticker ?? null}
            onSelect={selectOrToggle}
          />
        </div>
      </div>

      <div
        className={cn(
          "fixed inset-0 z-40 bg-black/30 transition-opacity duration-300 lg:hidden",
          selected ? "opacity-100" : "pointer-events-none opacity-0",
        )}
        onClick={() => setSelected(null)}
        aria-hidden="true"
      />

      <div
        className={cn(
          "fixed inset-x-0 bottom-0 z-50 flex max-h-[85vh] flex-col rounded-t-2xl border-t border-border bg-surface shadow-lg transition-transform duration-300 ease-out lg:hidden",
          selected ? "translate-y-0" : "translate-y-full",
          "pb-[env(safe-area-inset-bottom)]",
        )}
      >
        {/* Tap-to-close header. The wrapping button gives the drag
            indicator (and the empty space around it) a tap target so
            mobile users can dismiss the sheet without hunting for the
            tiny X icon — the visible drag bar already implies the
            interaction. Native swipe-to-close is a follow-up. */}
        <button
          type="button"
          className="relative flex w-full items-center justify-center px-4 pb-2 pt-3 focus-visible:ring-focus"
          onClick={() => setSelected(null)}
          aria-label="Close trade sheet"
        >
          <span className="block h-1 w-10 rounded-full bg-muted-foreground/30 transition-colors duration-150 group-hover:bg-muted-foreground/50" />
          <span
            className="absolute right-3 top-3 flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground hover:bg-surface-hover hover:text-foreground"
            aria-hidden="true"
          >
            <X size={16} />
          </span>
        </button>
        <div className="overflow-y-auto px-4 pb-4">
          <TradeTicket selection={selected} onClose={() => setSelected(null)} />
        </div>
      </div>

      {/* PAPER_PARLAY_SCOPE.md step 5/6 — operator-built parlay tray.
          Hidden when empty (returns null). Mounted at the page level
          so it survives selection changes. Step 6's dialog hooks
          onSave so the "Save paper parlay" button opens the save
          confirmation form. */}
      <ParlayTray onSave={() => setParlayDialogOpen(true)} />
      <PaperParlayDialog
        open={parlayDialogOpen}
        onOpenChange={setParlayDialogOpen}
      />
    </div>
  );
}
