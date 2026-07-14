"use client";

import { useState } from "react";
import { useParlayTray } from "@/components/parlays/parlay-tray-store";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { PickHistoryStrip } from "./pick-history-strip";
import { PredictionIntervalBand } from "./prediction-interval-band";
import { FreshnessBadge } from "./freshness-badge";
import type { FreshnessStaleGroup, PredictionInterval } from "@/lib/types";
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
  /** Smarter #21 phase 2d — prediction-interval diagnostic produced
   *  by the scoring kernel's interval consumer (PR 3). Renders as a
   *  horizontal band visualizing the [p10, p90] range with a tick at
   *  the market threshold. ``null`` when no trained interval sidecar
   *  exists for this stat key, or when the artifact lookup failed —
   *  the band component gracefully renders nothing in that case. */
  predictionInterval?: PredictionInterval | null;
  /** Smarter #22 PR A — Architecture #5 freshness diagnostics. List
   *  of feature groups that went stale for this recommendation.
   *  Empty / undefined when all groups are fresh (the common case);
   *  the badge component renders nothing in that case. */
  freshnessStaleGroups?: FreshnessStaleGroup[] | null;
  /** Smarter #22 PR A — total confidence penalty applied by the
   *  freshness layer for this recommendation. Sum of per-group
   *  ``confidence_delta`` values; ``null`` when no penalty was
   *  applied. */
  freshnessConfidenceDelta?: number | null;
}

interface TradeTicketProps {
  selection: TradeSelection | null;
  onClose?: () => void;
}

export function TradeTicket({
  selection,
  onClose,
}: TradeTicketProps) {
  // Demo-order entry was removed from the trade ticket on 2026-05-18 —
  // demo order is really a Kalshi-integration testing tool, not a daily
  // operator workflow, and it competed for attention with paper trade on
  // every card. The /demo-orders endpoint stays for ops + scripted use;
  // the dedicated /positions/demo page and the market-detail sheet still
  // surface it. See PAPER_PARLAY_SCOPE.md sibling discussion.
  const [tradeDialogOpen, setTradeDialogOpen] = useState(false);

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

  const prob = selection.selectedSideProbability;

  return (
    <>
      <div className="trade-ticket" data-testid="trade-ticket">
        <div className="space-y-1">
          <p className="gi-micro-label rail">ticket · {selection.eventName}</p>
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

        {/* Spec ticket instrument: 150px donut, fill = win probability. */}
        <div
          className="gi-donut"
          style={{ "--gd-p": prob != null ? Math.max(0, Math.min(100, prob * 100)) : 0 } as React.CSSProperties}
        >
          <span className="gi-donut-ring" aria-hidden />
          <span className="gi-donut-orbit" aria-hidden />
          <div className="gi-donut-center">
            <span className="gi-donut-value">{fmtPercent(prob)}</span>
            <span className="gi-micro-label">win probability</span>
          </div>
        </div>

        <div className="ticket-chip-grid">
          <div className="gi-stat-chip">
            <span className="k">{selection.selectedSide}</span>
            <span className="v">{fmtPrice(selection.entryPrice)}</span>
          </div>
          <div className="gi-stat-chip">
            <span className="k">edge</span>
            <span className={cn("v", selection.edge >= 0 ? "pos" : "neg")}>{fmtEdge(selection.edge)}</span>
          </div>
          <div className="gi-stat-chip">
            <span className="k">conf</span>
            <span className="v cyan">{fmtPercent(selection.confidence)}</span>
          </div>
        </div>

        {/* The outer guard is load-bearing — it controls whether the
           divider renders. Without it, removing the inner component
           guard wouldn't be enough; we'd still ship a divider with
           no band underneath when prediction_interval is null. */}
        {selection.predictionInterval && (
          <>
            <div className="ticket-section-divider" aria-hidden />
            <PredictionIntervalBand
              interval={selection.predictionInterval}
              statKey={selection.statKey}
            />
          </>
        )}

        {/* Same outer-guard pattern as the prediction-interval band:
           controls the divider, NOT redundant with the component's
           own empty-list guard. */}
        {selection.freshnessStaleGroups && selection.freshnessStaleGroups.length > 0 && (
          <>
            <div className="ticket-section-divider" aria-hidden />
            <FreshnessBadge
              staleGroups={selection.freshnessStaleGroups}
              confidenceDelta={selection.freshnessConfidenceDelta ?? null}
            />
          </>
        )}

        <div className="ticket-section-divider" aria-hidden />

        <PickHistoryStrip selection={selection} />

        <div className="ticket-section-divider" aria-hidden />

        <div className="grid gap-2">
          <button type="button" className="gi-btn" onClick={() => setTradeDialogOpen(true)}>
            Paper trade
          </button>
          <div className="ticket-ghost-row">
            <ParlayTrayButton selection={selection} />
            {selection.kalshiUrl && (
              <a
                className="gi-btn-ghost"
                href={selection.kalshiUrl}
                target="_blank"
                rel="noreferrer noopener"
              >
                kalshi ↗
              </a>
            )}
          </div>
        </div>

        {onClose && (
          <button type="button" className="gi-btn-ghost" onClick={onClose}>
            Close
          </button>
        )}
      </div>

      <TradeDialog
        open={tradeDialogOpen}
        onOpenChange={setTradeDialogOpen}
        defaults={{
          ticker: selection.ticker,
          // Bug #40 phase 7 — TradeDialogDefaults.side narrowed to "yes" | "no".
          // selection.selectedSide is typed string upstream; lowercase and cast.
          side: selection.selectedSide.toLowerCase() as "yes" | "no",
          price: selection.entryPrice ?? undefined,
        }}
        description="Open a paper trade for this pick without leaving the trade desk."
      />
    </>
  );
}

/**
 * PAPER_PARLAY_SCOPE.md step 5 — "Add to parlay" button on the trade
 * ticket. Calls into the module-level tray store from
 * ``components/parlays/parlay-tray-store.ts``. Disabled when the leg
 * is already in the tray (idempotent reassurance) or when the tray
 * has hit the MAX_TRAY_LEGS cap.
 *
 * Extracted as a sub-component so the parent ticket doesn't have to
 * subscribe to tray state — only this button (and the tray itself)
 * re-render on tray changes.
 */
function ParlayTrayButton({ selection }: { selection: TradeSelection }) {
  const { addLeg, removeLeg, contains, isFull } = useParlayTray();
  const alreadyInTray = contains(selection.ticker);
  // Toggle UX: if the leg is already in the tray, the button removes
  // it. Otherwise it adds — unless the tray is full, in which case
  // the button disables itself with a helpful label.
  if (alreadyInTray) {
    return (
      <button
        type="button"
        className="gi-btn-ghost"
        onClick={() => removeLeg(selection.ticker)}
        data-testid="ticket-parlay-toggle"
      >
        − parlay
      </button>
    );
  }
  return (
    <button
      type="button"
      className="gi-btn-ghost"
      onClick={() => addLeg(selection)}
      disabled={isFull}
      data-testid="ticket-parlay-toggle"
    >
      {isFull ? "Parlay full" : "+ parlay"}
    </button>
  );
}
