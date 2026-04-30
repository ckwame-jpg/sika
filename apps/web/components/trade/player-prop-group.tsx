"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";
import type { TradeDeskPlayerProp, TradeDeskThreshold } from "@/lib/types";
import { cn, fmtEdge, fmtPercent, fmtPrice } from "@/lib/utils";

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
  return statKey.replace(/_/g, " ").toLowerCase();
}

function formatDisplayText(value: string): string {
  return value.toLowerCase();
}

/**
 * Kalshi prop thresholds are single-direction — we only bet the OVER.
 * `selected_side` on TradeDeskThreshold is the canonical side, not a toggle.
 * Phase 1: skip any non-OVER thresholds (defensive; should never appear in practice).
 */
function isOverSide(side: string): boolean {
  const normalized = side.toLowerCase();
  return normalized === "over" || normalized === "yes";
}

function bestThresholdSummary(player: TradeDeskPlayerProp): ThresholdSummary | null {
  let best: ThresholdSummary | null = null;

  for (const group of player.stat_groups) {
    if (!thresholdsAreMonotonic(group.thresholds)) {
      continue;
    }
    const threshold = group.thresholds.find((item) => item.is_best && isOverSide(item.selected_side))
      ?? group.thresholds.find((item) => isOverSide(item.selected_side))
      ?? null;
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
      .filter((threshold) => threshold.ticker === selectedTicker && isOverSide(threshold.selected_side))
      .map((threshold) => ({
        statKey: group.stat_key,
        threshold,
      }))
  )[0] ?? null;
  const summary = selectedSummary ?? bestThresholdSummary(player);
  const visibleGroups = player.stat_groups
    .filter((group) => thresholdsAreMonotonic(group.thresholds))
    .map((group) => ({
      ...group,
      thresholds: group.thresholds
        .filter((threshold) => isOverSide(threshold.selected_side))
        .sort((left, right) => left.threshold - right.threshold),
    }))
    .filter((group) => group.thresholds.length > 0);

  return (
    <div
      className={cn("prop-group", expanded && "open")}
      data-testid="trade-prop-card"
    >
      <button
        type="button"
        className="prop-head"
        onClick={() => setExpanded((current) => !current)}
      >
        <ChevronRight className="prop-chev" aria-hidden />
        <div className="prop-head-player">
          <div className="prop-pname truncate">{formatDisplayText(player.subject_name)}</div>
          {player.subject_team && <div className="prop-psub">{formatDisplayText(player.subject_team)}</div>}
        </div>
        {summary && (
          <div className="prop-best-edge" aria-label="Selected ladder summary">
            <span className="label" data-testid="trade-prop-summary-label">
              {summary.threshold.threshold}+ {formatStatLabel(summary.statKey)}
            </span>
            <span
              className={summary.threshold.edge >= 0 ? "pos" : "neg"}
              data-testid="trade-prop-summary-edge"
            >
              {fmtEdge(summary.threshold.edge)}
            </span>
          </div>
        )}
      </button>

      {expanded && (
        <div className="prop-ladder">
          {visibleGroups.map((group) => {
            const selectedThreshold = group.thresholds.find((threshold) => threshold.ticker === selectedTicker);
            const rowSummary = selectedThreshold
              ?? group.thresholds.find((threshold) => threshold.is_best)
              ?? group.thresholds[0];
            const rowWinProb = rowSummary.selected_side_probability ?? rowSummary.probability_yes;
            return (
              <div className="prop-stat-row" key={group.stat_key}>
                <div className="prop-stat-meta">
                  <span className="prop-stat-name">{formatStatLabel(group.stat_key)}</span>
                  <span className="prop-stat-prob">{fmtPercent(rowWinProb)}</span>
                </div>
                <div className="prop-thresholds">
                  {group.thresholds.map((threshold) => {
                    const isSelected = threshold.ticker === selectedTicker;
                    const thresholdLabel = `${threshold.threshold}+`;
                    const winProb = threshold.selected_side_probability ?? threshold.probability_yes;
                    return (
                      <button
                        type="button"
                        key={threshold.ticker}
                        data-testid="trade-threshold-chip"
                        aria-pressed={isSelected}
                        aria-label={thresholdLabel}
                        title={`${formatStatLabel(group.stat_key)} ${thresholdLabel} · ${fmtPercent(winProb)} win · ${fmtPrice(threshold.entry_price)} entry · ${fmtEdge(threshold.edge)} edge`}
                        onClick={() =>
                          onSelectThreshold(
                            player.subject_name,
                            player.subject_team,
                            group.stat_key,
                            threshold,
                          )
                        }
                        className={cn("prop-threshold-chip", threshold.is_best && "best", isSelected && "selected")}
                      >
                        {thresholdLabel}
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
