"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { ChevronRight, RefreshCw, X } from "lucide-react";
import { fetchTradeDesk, keys } from "@/lib/api";
import type {
  TradeDeskArchivedSlate,
  TradeDeskEvent,
  TradeDeskGameLine,
  TradeDeskResponse,
  TradeDeskThreshold,
} from "@/lib/types";
import { cn, fmtDatetime, fmtEdge, fmtPercent, fmtPrice, fmtRelative, fmtStartsAt, sportLabel } from "@/lib/utils";
import { PlayerPropGroup } from "@/components/trade/player-prop-group";
import { TimeToCloseBadge } from "@/components/trade/time-to-close-badge";
import { TradeSelection, TradeTicket } from "@/components/trade/trade-ticket";
import { ProbabilitySurfaceHero } from "@/components/trade/probability-surface-hero";
import { Sparkline } from "@/components/ui/sparkline";

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
// the SPORT_TINTS map. The hsl literal is the historical visual default
// for unmapped sports in this surface; passing it explicitly preserves
// the prior look without re-duplicating the lookup table.
function sportTint(sport: string): string {
  return sharedSportTint(sport, "hsl(262 60% 70% / 0.6)");
}

function sectionOrder(marketKind: string) {
  if (marketKind === "game_winner" || marketKind === "first_five_winner") return 0;
  if (marketKind === "spread") return 1;
  if (marketKind === "total") return 2;
  return 99;
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
          <p className="text-[10px] uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
          <p className="mt-1 font-mono text-base text-foreground">{value}</p>
        </div>
      ))}
    </div>
  );
}

function GameLineRow({
  line,
  selectedTicker,
  onSelect,
}: {
  line: TradeDeskGameLine;
  selectedTicker?: string;
  onSelect: () => void;
}) {
  const isSelected = selectedTicker === line.ticker;
  const up = line.edge >= 0;
  // Bug #37: render real captured prices straight from the API.
  // ``Sparkline`` falls back to a flat baseline when ``price_history``
  // has fewer than two points, so cold-start markets (just discovered,
  // no MarketSnapshot rows yet) render as a flat line rather than a
  // synthetic walk that lied about the price trajectory.
  const series = line.price_history;
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn("line-row", isSelected && "selected")}
    >
      <div className="min-w-0">
        <div className="line-row-label truncate">{line.display_label}</div>
        <div className="line-row-lean">
          {line.projected_side_label
            ? `Model leans ${line.projected_side_label}`
            : `Selected side ${line.selected_side.toUpperCase()}`}
          <TimeToCloseBadge minutes={line.time_to_close_minutes} />
        </div>
      </div>
      <div className="line-row-price">{fmtPrice(line.entry_price)}</div>
      <div className="line-row-prob">{fmtPercent(line.selected_side_probability)}</div>
      <div className="line-row-spark">
        <Sparkline values={series} width={64} height={24} trend={up ? "up" : "down"} />
      </div>
      <div className={cn("line-row-edge", up ? "pos" : "neg")}>{fmtEdge(line.edge)}</div>
    </button>
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

interface KpiCardProps {
  label: string;
  value: string | number;
  sub?: string;
  testIdRoot: string;
  testIdValue: string;
}

function KpiCard({ label, value, sub, testIdRoot, testIdValue }: KpiCardProps) {
  return (
    <div className="trade-kpi" data-testid={testIdRoot}>
      <div className="trade-kpi-orb" aria-hidden />
      <div className="trade-kpi-label">{label}</div>
      <div className="trade-kpi-value" data-testid={testIdValue}>
        {value}
      </div>
      {sub && <div className="trade-kpi-sub">{sub}</div>}
    </div>
  );
}

function MarketSummaryStrip({ kpis }: { kpis: TradeKpis }) {
  return (
    <div className="trade-kpis">
      <KpiCard
        label="Events on the board"
        value={kpis.events}
        sub={`${kpis.live} live · ${kpis.upcoming} upcoming`}
        testIdRoot="trade-kpi-card-events"
        testIdValue="trade-kpi-events"
      />
      <KpiCard
        label="Candidate markets"
        value={kpis.candidateMarkets}
        sub="scored"
        testIdRoot="trade-kpi-card-candidate-markets"
        testIdValue="trade-kpi-candidate-markets"
      />
      <KpiCard
        label="Current picks"
        value={kpis.recommendations}
        sub="past edge threshold"
        testIdRoot="trade-kpi-card-recommendations"
        testIdValue="trade-kpi-recommendations"
      />
      <KpiCard
        label="Avg edge"
        value={kpis.avgEdge != null ? fmtEdge(kpis.avgEdge) : "—"}
        sub={
          kpis.topQuartileEdge != null
            ? `top-quartile ${fmtEdge(kpis.topQuartileEdge)}`
            : "top-quartile —"
        }
        testIdRoot="trade-kpi-card-avg-edge"
        testIdValue="trade-kpi-avg-edge"
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
  };
}

function countPropThresholds(event: TradeDeskEvent): number {
  return event.player_props.reduce(
    (total, player) =>
      total + player.stat_groups.reduce((groupTotal, group) => groupTotal + group.thresholds.length, 0),
    0,
  );
}

function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
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
        const showGameLines = event.game_lines.length > 0;
        const showPlayerProps = event.player_props.length > 0;
        const candidateCount = event.candidate_market_count ?? 0;
        const scoredCount = event.scored_market_count ?? 0;
        const coverageCount = event.coverage_prediction_count ?? 0;
        const showCoverageOnly = candidateCount > 0 || scoredCount > 0 || coverageCount > 0;
        if (!showGameLines && !showPlayerProps && !showCoverageOnly) {
          return null;
        }

        const groupedGameLines = [...event.game_lines].sort(
          (left, right) => sectionOrder(left.market_kind) - sectionOrder(right.market_kind),
        );
        const isExpanded = expandedEventIds.has(event.event_id);
        const marketsId = `${idPrefix}-trade-event-${event.event_id}-markets`;
        const selectedInEvent = selected?.eventId === event.event_id;
        const pickCount = event.game_lines.length + countPropThresholds(event);
        const ladderCount = event.player_props.length;
        const summaryParts = [
          pluralize(pickCount, "pick"),
          pluralize(ladderCount, "ladder"),
          coverageCount > 0 ? `${coverageCount} coverage` : null,
        ].filter(Boolean);

        return (
          <article
            key={`${idPrefix}-${event.event_id}`}
            className={cn("event-card", isExpanded && "open")}
            style={{ ["--sport-tint" as string]: sportTint(event.sport_key) }}
          >
            <button
              type="button"
              className="event-card-head event-card-toggle"
              aria-expanded={isExpanded}
              aria-controls={marketsId}
              onClick={() => onToggleEvent(event.event_id)}
              data-testid="trade-event-toggle"
            >
              <ChevronRight className="event-card-chev" size={16} aria-hidden />
              <span
                className="sport-pill"
                style={{ ["--tint" as string]: sportTint(event.sport_key) }}
              >
                <span className="dot" aria-hidden />
                {sportLabel(event.sport_key)}
              </span>
              <h2>{event.event_name}</h2>
              <span className="event-card-summary">
                {summaryParts.join(" · ")}
              </span>
              {selectedInEvent ? <span className="event-status-pill live">selected</span> : null}
              <span className="event-card-when">{fmtStartsAt(event.starts_at)}</span>
            </button>

            <div id={marketsId} className="event-card-markets" hidden={!isExpanded}>
              {isExpanded && showGameLines ? (
                <div className="market-section">
                  <div className="market-section-head">
                    <h3>Game Lines</h3>
                    <span className="count">{groupedGameLines.length} markets</span>
                  </div>
                  {groupedGameLines.map((line) => (
                    <GameLineRow
                      key={line.ticker}
                      line={line}
                      selectedTicker={selected?.ticker}
                      onSelect={() => onSelect(buildGameLineSelection(event, line))}
                    />
                  ))}
                </div>
              ) : null}

              {isExpanded && !showGameLines && !showPlayerProps && showCoverageOnly ? (
                <div className="market-section">
                  <div className="market-section-head">
                    <h3>Coverage</h3>
                    <span className="count">
                      {pluralize(scoredCount, "scored market")}
                    </span>
                  </div>
                  <div className="rounded-lg border border-border bg-surface-hover px-4 py-3 text-sm text-muted-foreground">
                    No bet cleared bet filters for this event.
                    {coverageCount > 0 ? ` ${coverageCount} coverage predictions were captured.` : ""}
                  </div>
                </div>
              ) : null}

              {isExpanded && showPlayerProps ? (
                <div className="market-section">
                  <div className="market-section-head">
                    <h3>Player Props</h3>
                    <span className="count">{event.player_props.length} ladders</span>
                  </div>
                  {event.player_props.map((player) => (
                    <PlayerPropGroup
                      key={`${idPrefix}-${event.event_id}-${player.subject_name}`}
                      player={player}
                      selectedTicker={selected?.ticker}
                      onSelectThreshold={(subjectName, subjectTeam, statKey, threshold) =>
                        onSelect(buildPlayerPropSelection(event, subjectName, subjectTeam, statKey, threshold))
                      }
                    />
                  ))}
                </div>
              ) : null}
            </div>
          </article>
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
    <section className={cn("archived-slate", expanded && "open")} data-testid="trade-previous-slate">
      <button
        type="button"
        className="archived-slate-head"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={onToggle}
      >
        <ChevronRight className="archived-slate-chev" size={16} aria-hidden />
        <span className="archived-slate-title">Last good slate · {fmtDatetime(slate.generated_at)}</span>
        <span className="archived-slate-summary">
          {slate.recommendation_count} picks · {slate.scored_market_count} scored
        </span>
      </button>
      <div id={panelId} className="archived-slate-body" hidden={!expanded}>
        <div className="archived-slate-kpis">
          <span>{slate.event_count} events</span>
          <span>{slate.candidate_market_count} candidates</span>
          <span>{slate.coverage_prediction_count} coverage</span>
          {slate.generated_from_run_id ? <span>run #{slate.generated_from_run_id}</span> : null}
        </div>
        <TradeEventList
          events={slate.events}
          expandedEventIds={expandedEventIds}
          idPrefix="archived"
          selected={selected}
          onToggleEvent={onToggleEvent}
          onSelect={onSelect}
        />
      </div>
    </section>
  );
}

export function TradeDesk({ sport }: { sport?: string }) {
  const [selected, setSelected] = useState<TradeSelection | null>(null);
  const [expandedEventIds, setExpandedEventIds] = useState<Set<number>>(() => new Set());
  const [archivedExpandedEventIds, setArchivedExpandedEventIds] = useState<Set<number>>(() => new Set());
  const [archiveState, setArchiveState] = useState<{ key: string | null; expanded: boolean } | null>(null);
  const { data, error, isLoading } = useSWR<TradeDeskResponse>(
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
    return (
      <div className="rounded-2xl border border-negative/30 bg-negative/10 px-4 py-6 text-center">
        <p className="text-sm font-medium text-foreground">Trade desk failed to load.</p>
        <p className="mt-1 text-sm text-muted-foreground">{error.message}</p>
      </div>
    );
  }

  if (!data) {
    return null;
  }

  const kpis = computeTradeKpis(data);
  const scoredCount = data.scored_market_count;
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
    <div className="space-y-4">
      <SlateStatusPill data={data} />
      <ProbabilitySurfaceHero
        scoredCount={scoredCount}
        recommendationCount={data.recommendation_count}
        avgEdge={kpis.avgEdge}
        topQuartileEdge={kpis.topQuartileEdge}
        generatedAt={data.generated_at}
      />
      <MarketSummaryStrip kpis={kpis} />

      <div className="flex gap-6">
        <div className="flex min-w-0 flex-1 flex-col gap-4">
          <TradeEventList
            events={data.events}
            expandedEventIds={expandedEventIds}
            idPrefix="current"
            selected={selected}
            onToggleEvent={toggleEvent}
            onSelect={selectOrToggle}
          />

          {data.events.length === 0 && (
            <div className="rounded-2xl border border-border bg-surface px-4 py-8 text-center">
              <p className="text-sm font-medium text-foreground">
                {data.freshness_status === "empty"
                  ? `No markets cleared thresholds${sport ? ` for ${sportLabel(sport)}` : ""}.`
                  : data.freshness_status === "degraded"
                    ? `Current slate is degraded${sport ? ` for ${sportLabel(sport)}` : ""}.`
                    : `No live trade-ready markets${sport ? ` for ${sportLabel(sport)}` : ""}.`}
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
                {data.blocking_reason || "NBA and MLB markets appear here when the current slate is populated."}
              </p>
              {(data.freshness_status === "degraded" || data.freshness_status === "empty") && (
                <SlateHealthDetails data={data} />
              )}
            </div>
          )}

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
              <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Research Only</p>
              <div className="grid gap-3 md:grid-cols-3">
                {data.research_sports.map((sportRow) => (
                  <div key={sportRow.sport_key} className="rounded-2xl border border-border bg-surface px-4 py-3">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-sm font-medium text-foreground">{sportLabel(sportRow.sport_key)}</p>
                      <span className="rounded-full border border-border px-2 py-0.5 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                        Research
                      </span>
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

        <div className="trade-ticket-rail hidden w-80 shrink-0 lg:block" data-testid="trade-ticket-rail">
          <TradeTicket selection={selected} />
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
        <div className="relative flex items-center justify-center px-4 pb-1 pt-3">
          <div className="h-1 w-10 rounded-full bg-muted-foreground/30" />
          <button
            className="absolute right-3 top-3 flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground hover:bg-surface-hover hover:text-foreground"
            onClick={() => setSelected(null)}
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>
        <div className="overflow-y-auto px-4 pb-4">
          <TradeTicket selection={selected} onClose={() => setSelected(null)} />
        </div>
      </div>
    </div>
  );
}
