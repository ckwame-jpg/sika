"use client";

import { ExternalLink } from "lucide-react";
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

function kalshiUrl(ticker?: string) {
  if (!ticker) return null;
  // Kalshi event tickers are the prefix before the last hyphen segment
  return `https://kalshi.com/markets/${ticker}`;
}

export function TradeTicket({
  marketTitle,
  subjectName,
  subjectTeam,
  statKey,
  threshold,
  ticker,
  onClose,
}: TradeTicketProps) {
  if (!threshold) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-border bg-surface p-6 text-center">
        <p className="text-sm text-muted-foreground">
          Select a player prop or game line to start
        </p>
      </div>
    );
  }

  const url = kalshiUrl(ticker ?? threshold.ticker);

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

      {url && (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className={cn(
            "flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition-colors",
            "bg-positive/90 text-positive-foreground hover:bg-positive",
          )}
        >
          Trade on Kalshi
          <ExternalLink size={13} />
        </a>
      )}

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
        {url && (
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-muted-foreground underline hover:text-foreground"
          >
            View on Kalshi
          </a>
        )}
      </div>
    </div>
  );
}
