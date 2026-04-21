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
import { cn, fmtEdge, fmtPercent, fmtPrice, fmtRelative, fmtStartsAt, sportLabel } from "@/lib/utils";
import { PlayerPropGroup } from "@/components/trade/player-prop-group";
import { TradeSelection, TradeTicket } from "@/components/trade/trade-ticket";
import { ProbabilitySurfaceHero } from "@/components/trade/probability-surface-hero";
import { Sparkline, randomWalk } from "@/components/ui/sparkline";

type MarketFilter = "all" | "player_props" | "game_lines";

interface TradeKpis {
  events: number;
  gameLines: number;
  propLadders: number;
  thresholds: number;
}

const SPORT_TINTS: Record<string, string> = {
  nba: "var(--sport-nba)",
  nfl: "var(--sport-nfl)",
  mlb: "var(--sport-mlb)",
  soccer: "var(--sport-soccer)",
  tennis: "var(--sport-tennis)",
  ufc: "var(--sport-ufc)",
};

function sportTint(sport: string): string {
  return SPORT_TINTS[sport.toLowerCase()] ?? "hsl(262 60% 70% / 0.6)";
}

function sectionOrder(marketKind: string) {
  if (marketKind === "game_winner" || marketKind === "first_five_winner") return 0;
  if (marketKind === "spread") return 1;
  if (marketKind === "total") return 2;
  return 99;
}

function seedFromString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i++) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) || 1;
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
  const series = randomWalk(20, up, seedFromString(line.ticker));
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

interface KpiCardProps {
  label: string;
  value: number;
  testIdRoot: string;
  testIdValue: string;
  seed: number;
  trend?: "up" | "down";
}

function KpiCard({ label, value, testIdRoot, testIdValue, seed, trend = "up" }: KpiCardProps) {
  const series = randomWalk(24, trend === "up", seed);
  return (
    <div className="trade-kpi" data-testid={testIdRoot}>
      <div className="trade-kpi-orb" aria-hidden />
      <div className="trade-kpi-label">{label}</div>
      <div className="trade-kpi-value" data-testid={testIdValue}>
        {value}
      </div>
      <div className="spark-wrap">
        <Sparkline values={series} width={140} height={22} trend={trend} />
      </div>
    </div>
  );
}

function MarketSummaryStrip({ kpis }: { kpis: TradeKpis }) {
  return (
    <div className="trade-kpis">
      <KpiCard
        label="Events"
        value={kpis.events}
        testIdRoot="trade-kpi-card-events"
        testIdValue="trade-kpi-events"
        seed={11}
      />
      <KpiCard
        label="Game Lines"
        value={kpis.gameLines}
        testIdRoot="trade-kpi-card-game-lines"
        testIdValue="trade-kpi-game-lines"
        seed={31}
      />
      <KpiCard
        label="Prop Ladders"
        value={kpis.propLadders}
        testIdRoot="trade-kpi-card-prop-ladders"
        testIdValue="trade-kpi-prop-ladders"
        seed={47}
      />
      <KpiCard
        label="Thresholds"
        value={kpis.thresholds}
        testIdRoot="trade-kpi-card-thresholds"
        testIdValue="trade-kpi-thresholds"
        seed={73}
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
  const scoredCount = data.scored_market_count || data.candidate_market_count;

  return (
    <div className="space-y-4">
      <SlateStatusPill data={data} />
      <ProbabilitySurfaceHero
        scoredCount={scoredCount}
        recommendationCount={data.recommendation_count}
      />
      <MarketSummaryStrip kpis={kpis} />

      {showFilterTabs && (
        <div className="market-filter-tabs">
          {[
            { value: "all", label: "All" },
            { value: "player_props", label: "Player Props" },
            { value: "game_lines", label: "Game Lines" },
          ].map((tab) => (
            <button
              key={tab.value}
              type="button"
              onClick={() => setMarketFilter(tab.value as MarketFilter)}
              className={cn(marketFilter === tab.value && "active")}
            >
              {tab.label}
            </button>
          ))}
        </div>
      )}

      <div className="flex gap-6">
        <div className="flex min-w-0 flex-1 flex-col gap-4">
          {data.events.map((event) => {
            const showGameLines = marketFilter !== "player_props" && event.game_lines.length > 0;
            const showPlayerProps = marketFilter !== "game_lines" && event.player_props.length > 0;
            if (!showGameLines && !showPlayerProps) {
              return null;
            }

            const groupedGameLines = [...event.game_lines].sort(
              (left, right) => sectionOrder(left.market_kind) - sectionOrder(right.market_kind),
            );

            return (
              <article
                key={event.event_id}
                className="event-card"
                style={{ ["--sport-tint" as string]: sportTint(event.sport_key) }}
              >
                <header className="event-card-head">
                  <span
                    className="sport-pill"
                    style={{ ["--tint" as string]: sportTint(event.sport_key) }}
                  >
                    <span className="dot" aria-hidden />
                    {sportLabel(event.sport_key)}
                  </span>
                  <h2>{event.event_name}</h2>
                  <span className="event-card-when">{fmtStartsAt(event.starts_at)}</span>
                </header>

                <div className="event-card-markets">
                  {showGameLines && (
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
                          onSelect={() =>
                            setSelected((current) =>
                              current?.ticker === line.ticker ? null : buildGameLineSelection(event, line),
                            )
                          }
                        />
                      ))}
                    </div>
                  )}

                  {showPlayerProps && (
                    <div className="market-section">
                      <div className="market-section-head">
                        <h3>Player Props</h3>
                        <span className="count">{event.player_props.length} ladders</span>
                      </div>
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
                </div>
              </article>
            );
          })}

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
