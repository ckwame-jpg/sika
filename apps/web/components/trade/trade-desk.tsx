"use client";

import { useState } from "react";
import useSWR from "swr";
import { RefreshCw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { PlayerPropGroup } from "@/components/trade/player-prop-group";
import { TradeTicket } from "@/components/trade/trade-ticket";
import { fetchTradeDesk, fetchWatchlist, keys } from "@/lib/api";
import type {
  RecommendationRead,
  TradeDeskEvent,
  TradeDeskResponse,
  TradeDeskThreshold,
} from "@/lib/types";
import { cn, fmtPercent, sportLabel } from "@/lib/utils";

type MarketFilter = "all" | "player_props" | "game_lines";

interface SelectedThreshold {
  subjectName: string;
  subjectTeam: string | null;
  statKey: string;
  threshold: TradeDeskThreshold;
  eventName: string;
}

function EventSection({ event }: { event: TradeDeskEvent }) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline gap-2">
        <h3 className="text-sm font-medium text-foreground">{event.event_name}</h3>
        {event.starts_at && (
          <span className="text-xs text-muted-foreground">
            {new Date(event.starts_at).toLocaleTimeString([], {
              hour: "numeric",
              minute: "2-digit",
            })}
          </span>
        )}
      </div>
    </div>
  );
}

function GameLineRow({ rec }: { rec: RecommendationRead }) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-border bg-surface px-4 py-3">
      <div className="flex items-baseline gap-2">
        <span className="text-sm text-foreground">
          {rec.display_market_title ?? rec.market_title}
        </span>
      </div>
      <div className="flex items-center gap-4">
        <span className="font-mono text-lg font-semibold text-foreground">
          {fmtPercent(rec.selected_side_probability ?? rec.confidence)}
        </span>
        <span
          className={cn(
            "font-mono text-xs font-medium",
            rec.edge > 0 ? "text-positive" : "text-muted-foreground",
          )}
        >
          {rec.edge > 0 ? "+" : ""}
          {(rec.edge * 100).toFixed(1)}%
        </span>
      </div>
    </div>
  );
}

/**
 * Client-side grouping fallback: transforms flat watchlist data into
 * trade desk format when the `/trade-desk` backend endpoint isn't available yet.
 */
function groupWatchlistForTradeDesk(
  items: RecommendationRead[],
): TradeDeskResponse {
  const eventMap = new Map<string, TradeDeskEvent>();

  for (const rec of items) {
    const eventKey = rec.event_name || "Other";
    if (!eventMap.has(eventKey)) {
      eventMap.set(eventKey, {
        event_name: eventKey,
        starts_at: rec.starts_at,
        sport_key: rec.sport_key ?? "",
        game_lines: [],
        player_props: [],
      });
    }
    const event = eventMap.get(eventKey)!;

    if (rec.market_family === "player_prop" && rec.subject_name) {
      let playerGroup = event.player_props.find(
        (p) => p.subject_name === rec.subject_name,
      );
      if (!playerGroup) {
        playerGroup = {
          subject_name: rec.subject_name,
          subject_team: rec.subject_team,
          stat_groups: [],
          best_edge: 0,
          best_win_prob: null,
        };
        event.player_props.push(playerGroup);
      }

      const statKey = rec.stat_key ?? "other";
      let statGroup = playerGroup.stat_groups.find(
        (sg) => sg.stat_key === statKey,
      );
      if (!statGroup) {
        statGroup = { stat_key: statKey, thresholds: [] };
        playerGroup.stat_groups.push(statGroup);
      }

      statGroup.thresholds.push({
        threshold: rec.threshold ?? 0,
        probability_yes: rec.selected_side_probability ?? rec.confidence,
        edge: rec.edge,
        entry_price: rec.suggested_price,
        ticker: rec.ticker,
        confidence: rec.confidence,
        selected_side_probability: rec.selected_side_probability,
        is_best: false,
      });

      // Update best edge/prob
      if (rec.edge > playerGroup.best_edge) {
        playerGroup.best_edge = rec.edge;
      }
      const prob = rec.selected_side_probability ?? rec.confidence;
      if (
        playerGroup.best_win_prob === null ||
        prob > playerGroup.best_win_prob
      ) {
        playerGroup.best_win_prob = prob;
      }
    } else {
      event.game_lines.push(rec);
    }
  }

  // Sort thresholds and mark best
  for (const event of eventMap.values()) {
    for (const player of event.player_props) {
      for (const sg of player.stat_groups) {
        sg.thresholds.sort((a, b) => a.threshold - b.threshold);
        let bestIdx = 0;
        for (let i = 1; i < sg.thresholds.length; i++) {
          if (sg.thresholds[i].edge > sg.thresholds[bestIdx].edge) {
            bestIdx = i;
          }
        }
        if (sg.thresholds.length > 0) {
          sg.thresholds[bestIdx].is_best = true;
        }
      }
    }
    // Sort players by best edge descending
    event.player_props.sort((a, b) => b.best_edge - a.best_edge);
  }

  return {
    events: Array.from(eventMap.values()).sort((a, b) => {
      if (!a.starts_at && !b.starts_at) return 0;
      if (!a.starts_at) return 1;
      if (!b.starts_at) return -1;
      return new Date(a.starts_at).getTime() - new Date(b.starts_at).getTime();
    }),
    research_sports: [],
  };
}

export function TradeDesk({ sport }: { sport?: string }) {
  const [selected, setSelected] = useState<SelectedThreshold | null>(null);
  const [marketFilter, setMarketFilter] = useState<MarketFilter>("all");

  // Try the trade-desk endpoint first, fall back to watchlist
  const { data: tradeDeskData, error: tradeDeskError } = useSWR<TradeDeskResponse>(
    keys.tradeDesk(sport),
    () => fetchTradeDesk(sport),
    { refreshInterval: 30_000 },
  );

  // Fallback: use watchlist data and group client-side
  const { data: watchlistData } = useSWR<RecommendationRead[]>(
    tradeDeskError ? keys.watchlist(sport, 100) : null,
    () => fetchWatchlist(sport, 100),
    { refreshInterval: 30_000 },
  );

  const data: TradeDeskResponse | null = tradeDeskData
    ?? (watchlistData ? groupWatchlistForTradeDesk(watchlistData) : null);

  if (!data) {
    return (
      <div className="flex items-center justify-center py-12">
        <RefreshCw size={16} className="animate-spin text-muted-foreground" />
      </div>
    );
  }

  // Toggle: clicking the already-selected threshold deselects it
  function handleSelectThreshold(
    subjectName: string,
    subjectTeam: string | null,
    statKey: string,
    threshold: TradeDeskThreshold,
    eventName: string,
  ) {
    if (selected?.threshold.ticker === threshold.ticker) {
      setSelected(null);
    } else {
      setSelected({ subjectName, subjectTeam, statKey, threshold, eventName });
    }
  }

  if (data.events.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
        <p className="text-sm text-muted-foreground">
          No live markets{sport ? ` for ${sportLabel(sport)}` : ""}
        </p>
        <p className="text-xs text-muted-foreground">
          Markets will appear here when events are available.
        </p>
      </div>
    );
  }

  const hasGameLines = data.events.some((e) => e.game_lines.length > 0);
  const hasPlayerProps = data.events.some((e) => e.player_props.length > 0);
  const showFilterTabs = hasGameLines && hasPlayerProps;

  return (
    <div className="space-y-4">
      {/* Market type filter tabs */}
      {showFilterTabs && (
        <div className="flex gap-1 rounded-lg border border-border bg-surface p-1">
          {(
            [
              { value: "all", label: "All" },
              { value: "player_props", label: "Player Props" },
              { value: "game_lines", label: "Game Lines" },
            ] as const
          ).map((tab) => (
            <button
              key={tab.value}
              onClick={() => setMarketFilter(tab.value)}
              className={cn(
                "flex-1 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
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
        {/* Left pane: event/prop ladders */}
        <div className="flex min-w-0 flex-1 flex-col gap-6">
          {data.events.map((event) => {
            const showGL =
              marketFilter !== "player_props" && event.game_lines.length > 0;
            const showPP =
              marketFilter !== "game_lines" && event.player_props.length > 0;

            if (!showGL && !showPP) return null;

            return (
              <div key={event.event_name} className="space-y-3">
                <EventSection event={event} />

                {/* Game lines */}
                {showGL && (
                  <div className="space-y-1.5">
                    <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                      Game Lines
                    </p>
                    {event.game_lines.map((gl) => (
                      <GameLineRow key={gl.id} rec={gl} />
                    ))}
                  </div>
                )}

                {/* Player props */}
                {showPP && (
                  <div className="space-y-2">
                    <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                      Player Props
                    </p>
                    {event.player_props.map((player) => (
                      <PlayerPropGroup
                        key={player.subject_name}
                        player={player}
                        selectedTicker={selected?.threshold.ticker}
                        onSelectThreshold={(name, team, statKey, threshold) =>
                          handleSelectThreshold(
                            name,
                            team,
                            statKey,
                            threshold,
                            event.event_name,
                          )
                        }
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}

          {/* Research sports */}
          {data.research_sports.length > 0 && (
            <div className="space-y-2">
              <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                Research
              </p>
              {data.research_sports.map((rs) => (
                <div
                  key={rs.sport_key}
                  className="flex items-center justify-between rounded-lg border border-border/60 bg-surface/50 px-4 py-3"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-foreground">
                      {sportLabel(rs.sport_key)}
                    </span>
                    <Badge variant="outline" className="text-[10px]">
                      Research
                    </Badge>
                  </div>
                  <div className="flex items-center gap-4 text-xs text-muted-foreground">
                    <span>{rs.events_count} events</span>
                    {rs.last_refresh_at && (
                      <span>
                        Last refresh:{" "}
                        {new Date(rs.last_refresh_at).toLocaleDateString()}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Right pane: sticky trade ticket (desktop only) */}
        <div className="sticky top-4 hidden w-72 shrink-0 self-start lg:block">
          <TradeTicket
            marketTitle={selected?.eventName ?? ""}
            subjectName={selected?.subjectName}
            subjectTeam={selected?.subjectTeam}
            statKey={selected?.statKey}
            threshold={selected?.threshold}
            ticker={selected?.threshold?.ticker}
          />
        </div>
      </div>

      {/* Mobile bottom sheet — backdrop */}
      <div
        className={cn(
          "fixed inset-0 z-40 bg-black/30 transition-opacity duration-300 lg:hidden",
          selected ? "opacity-100" : "pointer-events-none opacity-0",
        )}
        onClick={() => setSelected(null)}
        aria-hidden="true"
      />

      {/* Mobile bottom sheet — panel */}
      <div
        className={cn(
          "fixed inset-x-0 bottom-0 z-50 flex max-h-[85vh] flex-col rounded-t-2xl border-t border-border bg-surface shadow-lg transition-transform duration-300 ease-out lg:hidden",
          selected ? "translate-y-0" : "translate-y-full",
          "pb-[env(safe-area-inset-bottom)]",
        )}
      >
        <div className="relative flex items-center justify-center px-4 pt-3 pb-1">
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
          {selected && (
            <TradeTicket
              marketTitle={selected.eventName}
              subjectName={selected.subjectName}
              subjectTeam={selected.subjectTeam}
              statKey={selected.statKey}
              threshold={selected.threshold}
              ticker={selected.threshold.ticker}
              onClose={() => setSelected(null)}
            />
          )}
        </div>
      </div>
    </div>
  );
}
