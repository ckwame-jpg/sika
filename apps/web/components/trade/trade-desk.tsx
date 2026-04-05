"use client";

import { useState } from "react";
import useSWR from "swr";
import { RefreshCw, X } from "lucide-react";
import { fetchPositions, fetchTradeDesk, keys } from "@/lib/api";
import type {
  PositionsRead,
  TradeDeskEvent,
  TradeDeskGameLine,
  TradeDeskResponse,
  TradeDeskThreshold,
} from "@/lib/types";
import { cn, fmtContractPnl, fmtEdge, fmtPercent, fmtStartsAt, sportLabel } from "@/lib/utils";
import { PlayerPropGroup } from "@/components/trade/player-prop-group";
import { ExposureSummary, TradeSelection, TradeTicket } from "@/components/trade/trade-ticket";

type MarketFilter = "all" | "player_props" | "game_lines";

function emptyExposure(): ExposureSummary {
  return {
    openPositions: 0,
    openContracts: 0,
    pendingDemoOrders: 0,
    realizedPnl: null,
  };
}

function buildTickerExposureMap(data?: PositionsRead): Record<string, ExposureSummary> {
  const map: Record<string, ExposureSummary> = {};

  for (const position of data?.paper_positions ?? []) {
    const key = position.ticker;
    map[key] ??= emptyExposure();
    if (position.status === "open") {
      map[key].openPositions += 1;
      map[key].openContracts += position.quantity;
    } else if (position.pnl != null) {
      map[key].realizedPnl = (map[key].realizedPnl ?? 0) + position.pnl;
    }
  }

  for (const order of data?.demo_orders ?? []) {
    const key = order.ticker;
    map[key] ??= emptyExposure();
    if (order.status === "pending" || order.status === "resting") {
      map[key].pendingDemoOrders += 1;
    }
  }

  return map;
}

function exposureForTickers(data: PositionsRead | undefined, tickers: Set<string>): ExposureSummary {
  const summary = emptyExposure();
  for (const position of data?.paper_positions ?? []) {
    if (!tickers.has(position.ticker)) {
      continue;
    }
    if (position.status === "open") {
      summary.openPositions += 1;
      summary.openContracts += position.quantity;
    } else if (position.pnl != null) {
      summary.realizedPnl = (summary.realizedPnl ?? 0) + position.pnl;
    }
  }
  for (const order of data?.demo_orders ?? []) {
    if (!tickers.has(order.ticker)) {
      continue;
    }
    if (order.status === "pending" || order.status === "resting") {
      summary.pendingDemoOrders += 1;
    }
  }
  return summary;
}

function portfolioSummary(data?: PositionsRead) {
  const openPositions = (data?.paper_positions ?? []).filter((position) => position.status === "open");
  const realizedPnlValues = (data?.paper_positions ?? [])
    .filter((position) => position.status !== "open" && position.pnl != null)
    .map((position) => position.pnl ?? 0);
  const realizedPnl = realizedPnlValues.length > 0
    ? realizedPnlValues.reduce((total, pnl) => total + pnl, 0)
    : null;
  const pendingDemoOrders = (data?.demo_orders ?? []).filter((order) => order.status === "pending" || order.status === "resting");

  return {
    openPositions: openPositions.length,
    heldMarkets: new Set(openPositions.map((position) => position.ticker)).size,
    realizedPnl,
    pendingDemoOrders: pendingDemoOrders.length,
  };
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

function GameLineRow({
  line,
  selectedTicker,
  exposure,
  onSelect,
}: {
  line: TradeDeskGameLine;
  selectedTicker?: string;
  exposure?: ExposureSummary;
  onSelect: () => void;
}) {
  const hasExposure = (exposure?.openContracts ?? 0) > 0 || (exposure?.pendingDemoOrders ?? 0) > 0;

  return (
    <button
      onClick={onSelect}
      className={cn(
        "flex w-full items-center justify-between gap-4 rounded-2xl border px-4 py-3 text-left transition-colors",
        selectedTicker === line.ticker
          ? "border-accent bg-accent/10"
          : "border-border bg-surface hover:bg-surface-hover",
      )}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="truncate text-sm font-medium text-foreground">{line.display_label}</p>
          {hasExposure && (
            <span className="rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[10px] font-medium text-warning">
              {(exposure?.openContracts ?? 0) > 0 ? `Held ${exposure?.openContracts}` : `${exposure?.pendingDemoOrders} demo`}
            </span>
          )}
        </div>
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

function PortfolioSummaryStrip({ positions }: { positions?: PositionsRead }) {
  const summary = portfolioSummary(positions);

  return (
    <div className="grid gap-3 md:grid-cols-4">
      <div className="rounded-2xl border border-border bg-surface px-4 py-3">
        <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Open Positions</p>
        <p className="mt-1 font-mono text-xl font-semibold text-foreground">{summary.openPositions}</p>
      </div>
      <div className="rounded-2xl border border-border bg-surface px-4 py-3">
        <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Held Markets</p>
        <p className="mt-1 font-mono text-xl font-semibold text-foreground">{summary.heldMarkets}</p>
      </div>
      <div className="rounded-2xl border border-border bg-surface px-4 py-3">
        <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Pending Demo</p>
        <p className="mt-1 font-mono text-xl font-semibold text-foreground">{summary.pendingDemoOrders}</p>
      </div>
      <div className="rounded-2xl border border-border bg-surface px-4 py-3">
        <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">Realized PnL</p>
        <p
          className={cn(
            "mt-1 font-mono text-xl font-semibold",
            summary.realizedPnl == null
              ? "text-muted-foreground"
              : summary.realizedPnl >= 0
                ? "text-positive"
                : "text-negative",
          )}
        >
          {fmtContractPnl(summary.realizedPnl)}
        </p>
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
  const { data: positions } = useSWR<PositionsRead>(keys.positions, fetchPositions, {
    refreshInterval: 15_000,
  });

  const tickerExposureMap = buildTickerExposureMap(positions);
  const eventTickerMap = new Map<number, Set<string>>();
  for (const event of data?.events ?? []) {
    const tickers = new Set<string>();
    for (const line of event.game_lines) {
      tickers.add(line.ticker);
    }
    for (const player of event.player_props) {
      for (const statGroup of player.stat_groups) {
        for (const threshold of statGroup.thresholds) {
          tickers.add(threshold.ticker);
        }
      }
    }
    eventTickerMap.set(event.event_id, tickers);
  }

  const marketExposure = selected
    ? exposureForTickers(positions, new Set([selected.ticker]))
    : emptyExposure();
  const selectedEvent = data?.events.find((event) => event.event_id === selected?.eventId);
  const eventExposure = selectedEvent
    ? exposureForTickers(positions, eventTickerMap.get(selectedEvent.event_id) ?? new Set<string>())
    : emptyExposure();

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

  return (
    <div className="space-y-4">
      <PortfolioSummaryStrip positions={positions} />

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
                          exposure={tickerExposureMap[line.ticker]}
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
                        exposureByTicker={tickerExposureMap}
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
          <TradeTicket
            selection={selected}
            marketExposure={marketExposure}
            eventExposure={eventExposure}
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
          <TradeTicket
            selection={selected}
            marketExposure={marketExposure}
            eventExposure={eventExposure}
            onClose={() => setSelected(null)}
          />
        </div>
      </div>
    </div>
  );
}
