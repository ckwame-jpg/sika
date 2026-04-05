"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { cn, fmtPercent, fmtPrice } from "@/lib/utils";
import type { TradeDeskThreshold } from "@/lib/types";

interface TradeTicketProps {
  marketTitle: string;
  subjectName?: string;
  subjectTeam?: string | null;
  statKey?: string;
  threshold?: TradeDeskThreshold | null;
  ticker?: string;
  onClose?: () => void;
}

export function TradeTicket({
  marketTitle,
  subjectName,
  subjectTeam,
  statKey,
  threshold,
  onClose,
}: TradeTicketProps) {
  const [showAdvanced, setShowAdvanced] = useState(false);

  if (!threshold) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-border bg-surface p-6 text-center">
        <p className="text-sm text-muted-foreground">
          Select a player prop or game line to start
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4">
      <div className="space-y-0.5">
        <p className="text-xs text-muted-foreground">{marketTitle}</p>
        {subjectName && (
          <p className="text-sm font-medium text-foreground">
            {subjectName}
            {subjectTeam && (
              <span className="ml-1 text-xs text-muted-foreground">
                {subjectTeam}
              </span>
            )}
          </p>
        )}
        {statKey && (
          <p className="text-xs capitalize text-muted-foreground">
            {statKey.replace(/_/g, " ")} {threshold.threshold}+
          </p>
        )}
      </div>

      <div className="flex items-center gap-2">
        <Button size="sm" className="flex-1 bg-positive/90 hover:bg-positive text-positive-foreground">
          Buy Yes
        </Button>
        {showAdvanced && (
          <Button
            size="sm"
            variant="secondary"
            className="flex-1 border-negative/40 text-negative hover:bg-negative/10"
          >
            Buy No
          </Button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 text-center">
        <div className="rounded bg-positive/10 px-3 py-2">
          <p className="text-xs text-muted-foreground">Yes</p>
          <p className="font-mono text-sm font-medium text-positive">
            {fmtPrice(threshold.entry_price)}
          </p>
        </div>
        <div className="rounded bg-surface-hover px-3 py-2">
          <p className="text-xs text-muted-foreground">Win Prob</p>
          <p className="font-mono text-sm font-medium text-foreground">
            {fmtPercent(threshold.selected_side_probability ?? threshold.probability_yes)}
          </p>
        </div>
      </div>

      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          Edge:{" "}
          <span className={cn("font-mono font-medium", threshold.edge > 0 ? "text-positive" : "text-negative")}>
            {threshold.edge > 0 ? "+" : ""}
            {(threshold.edge * 100).toFixed(1)}%
          </span>
        </span>
        <button
          className="text-xs text-muted-foreground underline hover:text-foreground"
          onClick={() => setShowAdvanced(!showAdvanced)}
        >
          {showAdvanced ? "Simple" : "Advanced"}
        </button>
      </div>
    </div>
  );
}
