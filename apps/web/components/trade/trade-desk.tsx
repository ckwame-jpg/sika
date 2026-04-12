"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { RefreshCw, X } from "lucide-react";
import { fetchTradeDesk, keys } from "@/lib/api";
import type {
  TradeDeskEvent,
  TradeDeskGameLine,
  TradeDeskResponse,
  TradeDeskThreshold,
} from "@/lib/types";
import { cn, fmtEdge, fmtPercent, fmtRelative, fmtStartsAt, sportLabel } from "@/lib/utils";
import { PlayerPropGroup } from "@/components/trade/player-prop-group";
import { TradeSelection, TradeTicket } from "@/components/trade/trade-ticket";

type MarketFilter = "all" | "player_props" | "game_lines";

interface TradeKpis {
  events: number;
  gameLines: number;
  propLadders: number;
  thresholds: number;
}

function gameLineSectionLabel(marketKind: string) {
  if (marketKind === "spread") return "Spread";
  if (marketKind === "total") return "Total";
  if (marketKind === "first_five_winner") return "First 5";
  return "Winner";
}

function sectionOrder(marketKind: string) {
  if (marketKind === "game_winner" || marketKind === "first_five_winner") return 0;
  if (marketKind === "spread") return 1;
  if (marketKind === "total") return 2;
  return 99;
}

/**
 * Slice 3: per-surface freshness banner. Driven by ``freshness_status`` on
 * the trade-desk payload itself — no coupling to ``/health`` or any global
 * boundary. If the snapshot is stale the surface keeps rendering (the whole
 * point of the versioned, append-only snapshot store from Slice 2) and this
 * pill tells the user what they're looking at.
 */
function StaleSlatePill({ generatedAt }: { generatedAt: string | null }) {
  const relative = generatedAt ? fmtRelative(generatedAt) : null;
  return (
    <div
      className="flex items-center justify-between gap-3 rounded-2xl border border-warning/30 bg-warning/10 px-4 py-2 text-xs text-warning"
      role="status"
      data-testid="trade-desk-stale-pill"
    >
      <span className="font-medium">
        Showing last known slate
        {relative ? ` (snapshot ${relative})` : ""}. Refresh is behind.
      </span>
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
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "flex w-full items-center justify-between gap-4 rounded-2xl border px-4 py-3 text-left transition-colors",
        selectedTicker === line.ticker
          ? "border-accent bg-accent/10"
          : "border-border bg-surface hover:bg-surface-hover",
      )}
    >
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-foreground">{line.display_label}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {line.projected_side_label ? `Model leans ${line.projected_side_label}` : `Selected side ${line.selected_side.toUpperCase()}`}
        </p>
      </div>
      <div className="shrink-0 text-right">
        <p className="font-mono text-lg font-semibold text-foreground">{fmtPercent(line.selected_side_probability)}</p>
        <p className={cn("font-mono text-xs font-medium", line.edge >= 0 ? "text-positive" : "text-negative")}>
          {fmtEdge(line.edge)}
        </p>
      </div>
    </button>
  );
}

function computeTradeKpis(events: TradeDeskEvent[], marketFilter: MarketFilter): TradeKpis {
  const visibleEvents = events.filter(
    (event) =>
      (marketFilter !== "player_props" && event.game_lines.length > 0) ||
      (marketFilter !== "game_lines" && event.player_props.length > 0),
  );
  const gameLines = marketFilter === "player_props"
    ? 0
    : visibleEvents.reduce((total, event) => total + event.game_lines.length, 0);
  const propLadders = marketFilter === "game_lines"
    ? 0
    : visibleEvents.reduce(
      (total, event) => total + event.player_props.reduce((eventTotal, player) => eventTotal + player.stat_groups.length, 0),
      0,
    );
  const thresholds = marketFilter === "game_lines"
    ? 0
    : visibleEvents.reduce(
      (total, event) =>
        total + event.player_props.reduce(
          (eventTotal, player) =>
            eventTotal + player.stat_groups.reduce((groupTotal, group) => groupTotal + group.thresholds.length, 0),
          0,
        ),
      0,
    );

  return {
    events: visibleEvents.length,
    gameLines,
    propLadders,
    thresholds,
  };
}

function MarketSummaryStrip({ kpis }: { kpis: TradeKpis }) {
  return (
    <div className="grid gap-3 md:grid-cols-4">
      <div className="rounded-2xl border border-border bg-surface px-4 py-3" data-testid="trade-kpi-card-events">
        <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Events</p>
        <p className="mt-1 font-mono text-xl font-semibold text-foreground" data-testid="trade-kpi-events">{kpis.events}</p>
      </div>
      <div className="rounded-2xl border border-border bg-surface px-4 py-3" data-testid="trade-kpi-card-game-lines">
        <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Game Lines</p>
        <p className="mt-1 font-mono text-xl font-semibold text-foreground" data-testid="trade-kpi-game-lines">{kpis.gameLines}</p>
      </div>
      <div className="rounded-2xl border border-border bg-surface px-4 py-3" data-testid="trade-kpi-card-prop-ladders">
        <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Prop Ladders</p>
        <p className="mt-1 font-mono text-xl font-semibold text-foreground" data-testid="trade-kpi-prop-ladders">{kpis.propLadders}</p>
      </div>
      <div className="rounded-2xl border border-border bg-surface px-4 py-3" data-testid="trade-kpi-card-thresholds">
        <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Thresholds</p>
        <p className="mt-1 font-mono text-xl font-semibold text-foreground" data-testid="trade-kpi-thresholds">{kpis.thresholds}</p>
      </div>
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

export function TradeDesk({ sport }: { sport?: string }) {
  const [selected, setSelected] = useState<TradeSelection | null>(null);
  const [marketFilter, setMarketFilter] = useState<MarketFilter>("all");
  const { data, error, isLoading } = useSWR<TradeDeskResponse>(
    keys.tradeDesk(sport),
    () => fetchTradeDesk(sport),
    { refreshInterval: 30_000 },
  );

  useEffect(() => {
    if (!selected) {
      return;
    }
    if (marketFilter === "player_props" && selected.kind === "game_line") {
      setSelected(null);
      return;
    }
    if (marketFilter === "game_lines" && selected.kind === "player_prop") {
      setSelected(null);
    }
  }, [marketFilter, selected]);

  useEffect(() => {
    if (!selected || !data) {
      return;
    }

    const selectionStillVisible = data.events.some((event) =>
      event.game_lines.some((line) => line.ticker === selected.ticker) ||
      event.player_props.some((player) =>
        player.stat_groups.some((group) => group.thresholds.some((threshold) => threshold.ticker === selected.ticker))
      )
    );

    if (!selectionStillVisible) {
      setSelected(null);
    }
  }, [data, selected]);

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

  const hasGameLines = data.events.some((event) => event.game_lines.length > 0);
  const hasPlayerProps = data.events.some((event) => event.player_props.length > 0);
  const showFilterTabs = hasGameLines && hasPlayerProps;
  const kpis = computeTradeKpis(data.events, marketFilter);

  return (
    <div className="space-y-4">
      {data.freshness_status === "stale" && (
        <StaleSlatePill generatedAt={data.generated_at ?? null} />
      )}
      <MarketSummaryStrip kpis={kpis} />

      {showFilterTabs && (
        <div className="flex gap-1 rounded-2xl border border-border bg-surface p-1">
          {[
            { value: "all", label: "All" },
            { value: "player_props", label: "Player Props" },
            { value: "game_lines", label: "Game Lines" },
          ].map((tab) => (
            <button
              key={tab.value}
              onClick={() => setMarketFilter(tab.value as MarketFilter)}
              className={cn(
                "flex-1 rounded-xl px-3 py-2 text-xs font-medium transition-colors",
                marketFilter === tab.value
                  ? "bg-accent/15 text-accent"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {tab.label}
            </button>
          ))}
        </div>
      )}

      <div className="flex gap-6">
        <div className="flex min-w-0 flex-1 flex-col gap-6">
          {data.events.map((event) => {
            const showGameLines = marketFilter !== "player_props" && event.game_lines.length > 0;
            const showPlayerProps = marketFilter !== "game_lines" && event.player_props.length > 0;
            if (!showGameLines && !showPlayerProps) {
              return null;
            }

            const groupedGameLines = [...event.game_lines].sort(
              (left, right) => sectionOrder(left.market_kind) - sectionOrder(right.market_kind),
            );
            const sectionLabels = Array.from(new Set(groupedGameLines.map((line) => gameLineSectionLabel(line.market_kind))));

            return (
              <section key={event.event_id} className="space-y-3">
                <div className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-base font-semibold text-foreground">{event.event_name}</h3>
                    <span className="text-xs text-muted-foreground">{sportLabel(event.sport_key)}</span>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {fmtStartsAt(event.starts_at)} · {event.event_status.replace(/_/g, " ")}
                  </p>
                </div>

                {showGameLines && sectionLabels.map((sectionLabel) => {
                  const lines = groupedGameLines.filter((line) => gameLineSectionLabel(line.market_kind) === sectionLabel);
                  if (lines.length === 0) {
                    return null;
                  }
                  return (
                    <div key={`${event.event_id}-${sectionLabel}`} className="space-y-2">
                      <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">{sectionLabel}</p>
                      {lines.map((line) => (
                        <GameLineRow
                          key={line.ticker}
                          line={line}
                          selectedTicker={selected?.ticker}
                          onSelect={() =>
                            setSelected((current) =>
                              current?.ticker === line.ticker ? null : buildGameLineSelection(event, line),
                            )
                          }
                        />
                      ))}
                    </div>
                  );
                })}

                {showPlayerProps && (
                  <div className="space-y-2">
                    <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Player Props</p>
                    {event.player_props.map((player) => (
                      <PlayerPropGroup
                        key={`${event.event_id}-${player.subject_name}`}
                        player={player}
                        selectedTicker={selected?.ticker}
                        onSelectThreshold={(subjectName, subjectTeam, statKey, threshold) =>
                          setSelected((current) =>
                            current?.ticker === threshold.ticker
                              ? null
                              : buildPlayerPropSelection(event, subjectName, subjectTeam, statKey, threshold),
                          )
                        }
                      />
                    ))}
                  </div>
                )}
              </section>
            );
          })}

          {data.events.length === 0 && (
            <div className="rounded-2xl border border-border bg-surface px-4 py-8 text-center">
              <p className="text-sm font-medium text-foreground">
                No live trade-ready markets{sport ? ` for ${sportLabel(sport)}` : ""}.
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
                NBA and MLB markets appear here when the current slate is populated.
              </p>
            </div>
          )}

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

        <div className="sticky top-4 hidden w-80 shrink-0 self-start lg:block">
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
