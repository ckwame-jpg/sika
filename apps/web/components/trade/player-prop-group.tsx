"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { cn, fmtPercent } from "@/lib/utils";
import type { TradeDeskPlayerProp, TradeDeskThreshold } from "@/lib/types";

interface PlayerPropGroupProps {
  player: TradeDeskPlayerProp;
  onSelectThreshold: (
    subjectName: string,
    subjectTeam: string | null,
    statKey: string,
    threshold: TradeDeskThreshold,
  ) => void;
  selectedTicker?: string;
}

function ThresholdChip({
  threshold,
  isSelected,
  onClick,
}: {
  threshold: TradeDeskThreshold;
  isSelected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
        threshold.is_best
          ? "border-positive/50 bg-positive/10 text-positive"
          : "border-border bg-surface text-muted-foreground hover:bg-surface-hover hover:text-foreground",
        isSelected && "ring-1 ring-accent",
      )}
    >
      {threshold.threshold}+
    </button>
  );
}

/** Desktop: collapsible table row. Mobile: card with threshold pills. */
export function PlayerPropGroup({
  player,
  onSelectThreshold,
  selectedTicker,
}: PlayerPropGroupProps) {
  const [expanded, setExpanded] = useState(true);

  return (
    <div className="rounded-lg border border-border bg-surface">
      {/* Player header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-hover"
      >
        {expanded ? (
          <ChevronDown size={14} className="shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight size={14} className="shrink-0 text-muted-foreground" />
        )}
        <div className="flex flex-1 items-baseline gap-2">
          <span className="text-sm font-medium text-foreground">
            {player.subject_name}
          </span>
          {player.subject_team && (
            <span className="text-xs text-muted-foreground">
              {player.subject_team}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">
            Best edge:{" "}
            <span className={cn("font-mono font-medium", player.best_edge > 0 ? "text-positive" : "text-muted-foreground")}>
              {player.best_edge > 0 ? "+" : ""}
              {(player.best_edge * 100).toFixed(1)}%
            </span>
          </span>
          {player.best_win_prob != null && (
            <span className="font-mono text-sm font-medium text-foreground">
              {fmtPercent(player.best_win_prob)}
            </span>
          )}
        </div>
      </button>

      {/* Stat groups with threshold chips */}
      {expanded && (
        <div className="border-t border-border">
          {player.stat_groups.map((sg) => (
            <div
              key={sg.stat_key}
              className="flex flex-wrap items-center gap-3 border-b border-border/50 px-4 py-2.5 last:border-b-0"
            >
              <span className="w-20 shrink-0 text-xs font-medium capitalize text-muted-foreground">
                {sg.stat_key.replace(/_/g, " ")}
              </span>
              <div className="flex flex-wrap gap-1.5">
                {sg.thresholds.map((t) => (
                  <ThresholdChip
                    key={t.ticker}
                    threshold={t}
                    isSelected={selectedTicker === t.ticker}
                    onClick={() =>
                      onSelectThreshold(
                        player.subject_name,
                        player.subject_team,
                        sg.stat_key,
                        t,
                      )
                    }
                  />
                ))}
              </div>
              {/* Show the best threshold's probability prominently */}
              {sg.thresholds.find((t) => t.is_best) && (
                <span className="ml-auto font-mono text-lg font-semibold text-foreground">
                  {fmtPercent(
                    sg.thresholds.find((t) => t.is_best)!.selected_side_probability ??
                      sg.thresholds.find((t) => t.is_best)!.probability_yes,
                  )}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
