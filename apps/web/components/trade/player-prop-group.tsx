"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { TradeDeskPlayerProp, TradeDeskThreshold } from "@/lib/types";
import { cn, fmtEdge, fmtPercent } from "@/lib/utils";

interface PlayerPropGroupProps {
  player: TradeDeskPlayerProp;
  selectedTicker?: string;
  onSelectThreshold: (
    subjectName: string,
    subjectTeam: string | null,
    statKey: string,
    threshold: TradeDeskThreshold,
  ) => void;
}

interface ThresholdSummary {
  statKey: string;
  threshold: TradeDeskThreshold;
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

function formatStatLabel(statKey: string) {
  return statKey.replace(/_/g, " ");
}

function bestThresholdSummary(player: TradeDeskPlayerProp): ThresholdSummary | null {
  let best: ThresholdSummary | null = null;

  for (const group of player.stat_groups) {
    if (!thresholdsAreMonotonic(group.thresholds)) {
      continue;
    }
    const threshold = group.thresholds.find((item) => item.is_best) ?? group.thresholds[0] ?? null;
    if (!threshold) {
      continue;
    }
    if (!best || threshold.edge > best.threshold.edge) {
      best = {
        statKey: group.stat_key,
        threshold,
      };
    }
  }

  return best;
}

export function PlayerPropGroup({
  player,
  selectedTicker,
  onSelectThreshold,
}: PlayerPropGroupProps) {
  const [expanded, setExpanded] = useState(true);
  const selectedSummary = player.stat_groups.flatMap((group) =>
    group.thresholds
      .filter((threshold) => threshold.ticker === selectedTicker)
      .map((threshold) => ({
        statKey: group.stat_key,
        threshold,
      }))
  )[0] ?? null;
  const summary = selectedSummary ?? bestThresholdSummary(player);
  const summaryWinProb = summary
    ? summary.threshold.selected_side_probability ?? summary.threshold.probability_yes
    : null;

  return (
    <div className="rounded-2xl border border-border bg-surface" data-testid="trade-prop-card">
      <button
        type="button"
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
        {summary && (
          <div className="text-right">
            <p className="text-[11px] capitalize tracking-[0.08em] text-muted-foreground" data-testid="trade-prop-summary-label">
              {summary.threshold.threshold}+ {formatStatLabel(summary.statKey)}
            </p>
            <p
              className={cn("font-mono text-sm font-semibold", summary.threshold.edge >= 0 ? "text-positive" : "text-negative")}
              data-testid="trade-prop-summary-edge"
            >
              {fmtEdge(summary.threshold.edge)}
            </p>
            <p className="text-xs text-muted-foreground" data-testid="trade-prop-summary-win-prob">{fmtPercent(summaryWinProb)}</p>
          </div>
        )}
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
                    const isSelected = threshold.ticker === selectedTicker;
                    return (
                      <button
                        type="button"
                        key={threshold.ticker}
                        data-testid="trade-threshold-chip"
                        aria-pressed={isSelected}
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
                            ? "border-positive/40 bg-positive/10 text-positive"
                            : "border-border bg-surface-hover text-muted-foreground hover:text-foreground",
                        )}
                      >
                        <span>{threshold.threshold}+</span>
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
