"use client";

import { useState } from "react";
import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { PickHistoryStrip } from "./pick-history-strip";
import { cn, fmtEdge, fmtPercent, fmtPrice } from "@/lib/utils";

export interface TradeSelection {
  kind: "game_line" | "player_prop";
  ticker: string;
  eventId: number;
  marketTitle: string;
  eventName: string;
  sportKey: string;
  marketKind: string;
  displayLabel: string;
  projectedSideLabel: string | null;
  selectedSide: string;
  selectedSideProbability: number | null;
  entryPrice: number | null;
  edge: number;
  confidence: number;
  kalshiUrl: string | null;
  subjectName?: string;
  subjectTeam?: string | null;
  statKey?: string;
  threshold?: number | null;
  /** Signed numeric line for spread/total game-line picks (pre-signed
   *  on the backend from the picked side's perspective). Null for
   *  moneyline + player_prop selections. */
  numericLine?: number | null;
  /** Effective over/under direction the pick represents — folds in
   *  the market's ``copilot_direction`` so the strip can color outcomes
   *  correctly for Under-direction total markets (codex round-1 P2 on
   *  PR #24). Null for non-total markets. */
  totalDirection?: "over" | "under" | null;
}

interface TradeTicketProps {
  selection: TradeSelection | null;
  onClose?: () => void;
}

export function TradeTicket({
  selection,
  onClose,
}: TradeTicketProps) {
  const [tradeDestination, setTradeDestination] = useState<"paper" | "demo" | null>(null);

  if (!selection) {
    return (
      <div className="trade-ticket empty">
        <div className="trade-ticket-empty-orb" aria-hidden>
          <div className="trade-ticket-empty-orb-core" />
        </div>
        <p className="text-sm text-muted-foreground">Pick a market.</p>
      </div>
    );
  }

  return (
    <>
      <div className="trade-ticket" data-testid="trade-ticket">
        <div className="space-y-1">
          <p className="ticket-eyebrow">{selection.eventName}</p>
          <h3 className="ticket-title" data-testid="trade-ticket-title">
            {selection.displayLabel}
          </h3>
          <p className="ticket-lean">
            {selection.projectedSideLabel
              ? `Model leans ${selection.projectedSideLabel}`
              : `Selected side: ${selection.selectedSide.toUpperCase()}`}
          </p>
          {selection.subjectName && selection.statKey && selection.threshold != null && (
            <p className="ticket-meta capitalize">
              {selection.subjectName}
              {selection.subjectTeam ? ` · ${selection.subjectTeam}` : ""}
              {` · ${selection.statKey.replace(/_/g, " ")} ${selection.threshold}+`}
            </p>
          )}
        </div>

        <div className="ticket-pair">
          <div className="ticket-stat">
            <p className="ticket-stat-label">{selection.selectedSide.toUpperCase()}</p>
            <p className="ticket-stat-value">{fmtPrice(selection.entryPrice)}</p>
          </div>
          <div className="ticket-stat">
            <p className="ticket-stat-label">Win Prob</p>
            <p className="ticket-stat-value">{fmtPercent(selection.selectedSideProbability)}</p>
          </div>
          <div className="ticket-stat">
            <p className="ticket-stat-label">Edge</p>
            <p className={cn("ticket-stat-value", selection.edge >= 0 ? "pos" : "neg")}>
              {fmtEdge(selection.edge)}
            </p>
          </div>
          <div className="ticket-stat">
            <p className="ticket-stat-label">Confidence</p>
            <p className="ticket-stat-value accent">{fmtPercent(selection.confidence)}</p>
          </div>
        </div>

        <div className="ticket-section-divider" aria-hidden />

        <PickHistoryStrip selection={selection} />

        <div className="ticket-section-divider" aria-hidden />

        <div className="grid gap-2">
          <Button variant="primary" size="sm" onClick={() => setTradeDestination("paper")}>
            Paper trade
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setTradeDestination("demo")}>
            Demo order
          </Button>
          {selection.kalshiUrl && (
            <Button variant="ghost" size="sm" asChild>
              <a href={selection.kalshiUrl} target="_blank" rel="noreferrer noopener">
                Trade on Kalshi
                <ExternalLink size={13} className="ml-1.5" />
              </a>
            </Button>
          )}
        </div>

        {onClose && (
          <Button variant="ghost" size="sm" onClick={onClose}>
            Close
          </Button>
        )}
      </div>

      <TradeDialog
        open={tradeDestination !== null}
        onOpenChange={(open) => {
          if (!open) {
            setTradeDestination(null);
          }
        }}
        defaults={{
          destination: tradeDestination ?? "paper",
          ticker: selection.ticker,
          side: selection.selectedSide,
          price: selection.entryPrice ?? undefined,
        }}
        description="Route this pick to paper or demo without leaving the trade desk."
      />
    </>
  );
}
