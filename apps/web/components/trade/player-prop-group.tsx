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
  return statKey.replace(/_/g, " ");
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

function initialsOf(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
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
  const summaryWinProb = summary
    ? summary.threshold.selected_side_probability ?? summary.threshold.probability_yes
    : null;
  const subtitle = [player.subject_team, summary ? formatStatLabel(summary.statKey) : null]
    .filter(Boolean)
    .join(" · ");

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
          <span className="prop-avatar" aria-hidden>
            {initialsOf(player.subject_name)}
          </span>
          <div className="min-w-0">
            <div className="prop-pname truncate">{player.subject_name}</div>
            {subtitle && <div className="prop-psub">{subtitle}</div>}
          </div>
        </div>
        {summary && (
          <div className="prop-best-edge">
            <span className="label">best edge</span>
            <span
              className={summary.threshold.edge >= 0 ? "pos" : "neg"}
              data-testid="trade-prop-summary-edge"
            >
              {fmtEdge(summary.threshold.edge)}
            </span>
            <span className="label" data-testid="trade-prop-summary-label">
              {summary.threshold.threshold}+ {formatStatLabel(summary.statKey)}
            </span>
            <span className="label" data-testid="trade-prop-summary-win-prob">
              {fmtPercent(summaryWinProb)}
            </span>
          </div>
        )}
      </button>

      {expanded && (
        <div className="prop-ladder">
          {player.stat_groups.map((group) => {
            if (!thresholdsAreMonotonic(group.thresholds)) {
              return null;
            }

            return group.thresholds
              .filter((threshold) => isOverSide(threshold.selected_side))
              .map((threshold) => {
                const isSelected = threshold.ticker === selectedTicker;
                const winProb = threshold.selected_side_probability ?? threshold.probability_yes;
                const thresholdLabel = `${threshold.threshold}+`;
                return (
                  <button
                    type="button"
                    key={threshold.ticker}
                    data-testid="trade-threshold-chip"
                    aria-pressed={isSelected}
                    aria-label={thresholdLabel}
                    onClick={() =>
                      onSelectThreshold(
                        player.subject_name,
                        player.subject_team,
                        group.stat_key,
                        threshold,
                      )
                    }
                    className={cn("ladder-row", "over", isSelected && "selected")}
                  >
                    <span className="side-pill">OVER</span>
                    <span className="thresh">
                      <span className="u">{formatStatLabel(group.stat_key)}</span>
                      <span>{thresholdLabel}</span>
                    </span>
                    <span className="p">{fmtPrice(threshold.entry_price)}</span>
                    <span className="wp">{fmtPercent(winProb)}</span>
                    <span className={cn("ed", threshold.edge >= 0 ? "pos" : "neg")}>
                      {fmtEdge(threshold.edge)}
                    </span>
                  </button>
                );
              });
          })}
        </div>
      )}
    </div>
  );
}
