"use client";

import { useState } from "react";
import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { cn, fmtContractPnl, fmtEdge, fmtPercent, fmtPrice } from "@/lib/utils";

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
}

export interface ExposureSummary {
  openPositions: number;
  openContracts: number;
  pendingDemoOrders: number;
  realizedPnl: number | null;
}

interface TradeTicketProps {
  selection: TradeSelection | null;
  marketExposure: ExposureSummary;
  eventExposure: ExposureSummary;
  onClose?: () => void;
}

function ExposureCard({
  title,
  summary,
  emptyMessage,
}: {
  title: string;
  summary: ExposureSummary;
  emptyMessage: string;
}) {
  const hasExposure =
    summary.openPositions > 0 ||
    summary.openContracts > 0 ||
    summary.pendingDemoOrders > 0 ||
    summary.realizedPnl != null;

  return (
    <div className="rounded-lg border border-border bg-surface/80 p-3">
      <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">{title}</p>
      {hasExposure ? (
        <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
          <div>
            <p className="text-xs text-muted-foreground">Open</p>
            <p className="font-mono text-foreground">{summary.openPositions}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Contracts</p>
            <p className="font-mono text-foreground">{summary.openContracts}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Pending demo</p>
            <p className="font-mono text-foreground">{summary.pendingDemoOrders}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Realized PnL</p>
            <p
              className={cn(
                "font-mono",
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
      ) : (
        <p className="mt-2 text-sm text-muted-foreground">{emptyMessage}</p>
      )}
    </div>
  );
}

export function TradeTicket({
  selection,
  marketExposure,
  eventExposure,
  onClose,
}: TradeTicketProps) {
  const [tradeDestination, setTradeDestination] = useState<"paper" | "demo" | null>(null);

  if (!selection) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-2xl border border-border bg-surface p-6 text-center">
        <p className="text-sm text-muted-foreground">Select a player prop or game line to start.</p>
      </div>
    );
  }

  return (
    <>
      <div className="flex flex-col gap-4 rounded-2xl border border-border bg-surface p-4">
        <div className="space-y-1">
          <p className="text-xs uppercase tracking-[0.14em] text-muted-foreground">{selection.eventName}</p>
          <h3 className="text-lg font-semibold text-foreground">{selection.displayLabel}</h3>
          <p className="text-sm text-muted-foreground">
            {selection.projectedSideLabel
              ? `Model leans ${selection.projectedSideLabel}`
              : `Selected side: ${selection.selectedSide.toUpperCase()}`}
          </p>
          {selection.subjectName && selection.statKey && selection.threshold != null && (
            <p className="text-xs capitalize text-muted-foreground">
              {selection.subjectName}
              {selection.subjectTeam ? ` · ${selection.subjectTeam}` : ""}
              {` · ${selection.statKey.replace(/_/g, " ")} ${selection.threshold}+`}
            </p>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="rounded-xl border border-border bg-surface-hover px-3 py-2.5">
            <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
              {selection.selectedSide.toUpperCase()}
            </p>
            <p className="mt-1 font-mono text-lg font-semibold text-foreground">
              {fmtPrice(selection.entryPrice)}
            </p>
          </div>
          <div className="rounded-xl border border-border bg-surface-hover px-3 py-2.5">
            <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">Win Prob</p>
            <p className="mt-1 font-mono text-lg font-semibold text-foreground">
              {fmtPercent(selection.selectedSideProbability)}
            </p>
          </div>
          <div className="rounded-xl border border-border bg-surface-hover px-3 py-2.5">
            <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">Edge</p>
            <p className={cn("mt-1 font-mono text-lg font-semibold", selection.edge >= 0 ? "text-positive" : "text-negative")}>
              {fmtEdge(selection.edge)}
            </p>
          </div>
          <div className="rounded-xl border border-border bg-surface-hover px-3 py-2.5">
            <p className="text-xs uppercase tracking-[0.12em] text-muted-foreground">Confidence</p>
            <p className="mt-1 font-mono text-lg font-semibold text-foreground">
              {fmtPercent(selection.confidence)}
            </p>
          </div>
        </div>

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

        <ExposureCard
          title="Your Exposure"
          summary={marketExposure}
          emptyMessage="No open paper position or pending demo order on this market."
        />
        <ExposureCard
          title="Event Context"
          summary={eventExposure}
          emptyMessage="No portfolio exposure on this event yet."
        />

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
