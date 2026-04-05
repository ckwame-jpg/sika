"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { TradeDeskPlayerProp, TradeDeskThreshold } from "@/lib/types";
import { cn, fmtEdge, fmtPercent } from "@/lib/utils";

interface ExposureBadge {
  openContracts: number;
  pendingDemoOrders: number;
}

interface PlayerPropGroupProps {
  player: TradeDeskPlayerProp;
  selectedTicker?: string;
  exposureByTicker: Record<string, ExposureBadge>;
  onSelectThreshold: (
    subjectName: string,
    subjectTeam: string | null,
    statKey: string,
    threshold: TradeDeskThreshold,
  ) => void;
}

function thresholdsAreMonotonic(thresholds: TradeDeskThreshold[]): boolean {
  let previousProbability: number | null = null;
  for (const threshold of [...thresholds].sort((left, right) => left.threshold - right.threshold)) {
    if (previousProbability != null && threshold.probability_yes > previousProbability + 1e-9) {
      return false;
    }
    previousProbability = threshold.probability_yes;
  }
  return true;
}

function ExposurePill({ exposure }: { exposure?: ExposureBadge }) {
  if (!exposure || (exposure.openContracts === 0 && exposure.pendingDemoOrders === 0)) {
    return null;
  }

  const label = exposure.openContracts > 0
    ? `Held ${exposure.openContracts}`
    : `${exposure.pendingDemoOrders} demo`;

  return (
    <span className="rounded-full border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning">
      {label}
    </span>
  );
}

export function PlayerPropGroup({
  player,
  selectedTicker,
  exposureByTicker,
  onSelectThreshold,
}: PlayerPropGroupProps) {
  const [expanded, setExpanded] = useState(true);

  return (
    <div className="rounded-2xl border border-border bg-surface">
      <button
        onClick={() => setExpanded((current) => !current)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-hover"
      >
        {expanded ? (
          <ChevronDown size={14} className="shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight size={14} className="shrink-0 text-muted-foreground" />
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-foreground">{player.subject_name}</p>
          {player.subject_team && (
            <p className="text-xs text-muted-foreground">{player.subject_team}</p>
          )}
        </div>
        <div className="text-right">
          <p className={cn("font-mono text-sm font-semibold", player.best_edge >= 0 ? "text-positive" : "text-negative")}>
            {fmtEdge(player.best_edge)}
          </p>
          <p className="text-xs text-muted-foreground">{fmtPercent(player.best_win_prob)}</p>
        </div>
      </button>

      {expanded && (
        <div className="border-t border-border">
          {player.stat_groups.map((group) => {
            if (!thresholdsAreMonotonic(group.thresholds)) {
              return null;
            }

            const selectedThreshold = group.thresholds.find((threshold) => threshold.ticker === selectedTicker);
            const displayThreshold = selectedThreshold ?? group.thresholds.find((threshold) => threshold.is_best) ?? group.thresholds[0];

            return (
              <div
                key={`${player.subject_name}-${group.stat_key}`}
                className="flex flex-wrap items-center gap-3 border-b border-border/60 px-4 py-3 last:border-b-0"
              >
                <div className="w-24 shrink-0">
                  <p className="text-xs font-medium capitalize text-muted-foreground">
                    {group.stat_key.replace(/_/g, " ")}
                  </p>
                  <p className="text-[11px] text-muted-foreground">
                    {fmtPercent(displayThreshold?.selected_side_probability ?? displayThreshold?.probability_yes)}
                  </p>
                </div>
                <div className="flex flex-1 flex-wrap gap-1.5">
                  {group.thresholds.map((threshold) => {
                    const exposure = exposureByTicker[threshold.ticker];
                    const isSelected = threshold.ticker === selectedTicker;
                    return (
                      <button
                        key={threshold.ticker}
                        onClick={() =>
                          onSelectThreshold(
                            player.subject_name,
                            player.subject_team,
                            group.stat_key,
                            threshold,
                          )
                        }
                        className={cn(
                          "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                          isSelected
                            ? "border-accent bg-accent/15 text-accent"
                            : threshold.is_best
                              ? "border-positive/40 bg-positive/10 text-positive"
                              : "border-border bg-surface-hover text-muted-foreground hover:text-foreground",
                        )}
                      >
                        <span>{threshold.threshold}+</span>
                        <ExposurePill exposure={exposure} />
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
