"use client";

import { useState } from "react";
import useSWR, { mutate } from "swr";
import { ChevronDown, ChevronRight, Trash2 } from "lucide-react";
import { deletePaperParlay, fetchPositions, keys } from "@/lib/api";
import type { PaperParlayRead, PositionsRead } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { TruncationHint } from "@/components/positions/truncation-hint";
import { fmtDatetime, pnlClass } from "@/lib/utils";
import { cn } from "@/lib/utils";

/**
 * PAPER_PARLAY_SCOPE.md step 7 — portfolio table for operator-built
 * paper parlays. Mirrors PaperPositionsTable's shape so the
 * portfolio page renders all three sika "fake-money" surfaces
 * (positions, demo orders, parlays) with the same visual rhythm.
 *
 * Each row expands to a detail panel showing per-leg state
 * (ticker, side, entry price, computed cushion). Settled parlays
 * surface the realized PnL with the same green/red coloring as
 * paper positions; pending parlays show their current leg-count
 * status.
 *
 * Data source: the /positions aggregator (added in step 3). Same
 * SWR key as PaperPositionsTable so a successful save in the dialog
 * (step 6) triggers a single refetch that updates both tables.
 */
export function PaperParlaysTable() {
  const { data, isLoading } = useSWR<PositionsRead>(keys.positions, () => fetchPositions());
  const [expandedId, setExpandedId] = useState<number | null>(null);

  if (isLoading) {
    return (
      <div className="px-4 py-6">
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  const parlays = data?.paper_parlays ?? [];
  if (parlays.length === 0) {
    return (
      <div className="px-4 py-8 text-center text-sm text-muted-foreground" data-testid="paper-parlays-empty">
        No paper parlays yet. Build one from the trade desk.
      </div>
    );
  }

  return (
    <>
      <Table data-testid="paper-parlays-table">
        <TableHeader>
          <TableRow>
            <TableHead className="w-10" />
            <TableHead>Created</TableHead>
            <TableHead>Sport</TableHead>
            <TableHead className="text-right">Legs</TableHead>
            <TableHead className="text-right">Combined</TableHead>
            <TableHead className="text-right">Odds</TableHead>
            <TableHead className="text-right">Stake</TableHead>
            <TableHead className="text-right">Status</TableHead>
            <TableHead className="text-right">PnL</TableHead>
            <TableHead className="w-10 text-right" aria-label="Actions" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {parlays.map((parlay) => (
            <ParlayRow
              key={parlay.id}
              parlay={parlay}
              expanded={expandedId === parlay.id}
              onToggle={() =>
                setExpandedId((current) => (current === parlay.id ? null : parlay.id))
              }
            />
          ))}
        </TableBody>
      </Table>
      {data?.paper_parlays_truncated && (
        <TruncationHint visibleCount={parlays.length} limitParam="paper_limit" />
      )}
    </>
  );
}

interface ParlayRowProps {
  parlay: PaperParlayRead;
  expanded: boolean;
  onToggle: () => void;
}

function ParlayRow({ parlay, expanded, onToggle }: ParlayRowProps) {
  const outcomeTone = outcomeToneClass(parlay.outcome);
  const ChevronIcon = expanded ? ChevronDown : ChevronRight;

  return (
    <>
      <TableRow
        data-testid={`paper-parlay-row-${parlay.id}`}
        className="cursor-pointer hover:bg-surface-hover/40"
        onClick={onToggle}
      >
        <TableCell className="w-10 align-middle">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
            aria-label={expanded ? "Collapse legs" : "Expand legs"}
            aria-expanded={expanded}
            className="flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:bg-surface-hover hover:text-foreground focus-visible:ring-focus"
          >
            <ChevronIcon size={14} />
          </button>
        </TableCell>
        <TableCell className="font-mono text-xs">{fmtDatetime(parlay.created_at)}</TableCell>
        <TableCell className="text-xs">{parlay.sport_scope}</TableCell>
        <TableCell className="text-right font-mono text-xs">{parlay.leg_count}</TableCell>
        <TableCell className="text-right font-mono text-xs">{parlay.combined_market_price.toFixed(2)}</TableCell>
        <TableCell className="text-right font-mono text-xs">{parlay.american_odds}</TableCell>
        <TableCell className="text-right font-mono text-xs">${parlay.stake.toFixed(2)}</TableCell>
        <TableCell className="text-right">
          <span className={cn("paper-parlay-status-pill", outcomeTone)} data-testid={`paper-parlay-status-${parlay.id}`}>
            {parlay.outcome}
          </span>
        </TableCell>
        <TableCell className="text-right">
          <PnlCell pnl={parlay.realized_pnl} outcome={parlay.outcome} />
        </TableCell>
        <TableCell className="w-10 text-right">
          <DeleteButton parlayId={parlay.id} />
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow data-testid={`paper-parlay-detail-${parlay.id}`}>
          <TableCell colSpan={10} className="bg-surface/40 px-6 py-4">
            <ParlayDetail parlay={parlay} />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function ParlayDetail({ parlay }: { parlay: PaperParlayRead }) {
  return (
    <div className="grid gap-3">
      {parlay.notes && (
        <p className="text-xs text-muted-foreground italic">"{parlay.notes}"</p>
      )}
      <div className="grid gap-1.5">
        <span className="text-3xs uppercase tracking-[0.14em] text-muted-foreground/70">Legs</span>
        <ol className="grid gap-1">
          {parlay.legs.map((leg) => (
            <li
              key={leg.id}
              className="flex items-baseline justify-between gap-3 rounded border border-border/60 bg-surface-hover/30 px-3 py-1.5 text-xs"
            >
              <span className="flex items-baseline gap-2">
                <span className="font-mono text-muted-foreground">#{leg.leg_index + 1}</span>
                <span>{legSummary(leg)}</span>
              </span>
              <span className="font-mono text-muted-foreground">
                {leg.side.toUpperCase()} @ {leg.suggested_price.toFixed(2)}
              </span>
            </li>
          ))}
        </ol>
      </div>
      {parlay.settlement_notes && (
        <p className="text-xs text-muted-foreground">{parlay.settlement_notes}</p>
      )}
    </div>
  );
}

function PnlCell({ pnl, outcome }: { pnl: number | null; outcome: string }) {
  if (outcome === "pending" || outcome === "unresolved") {
    return <span className="font-mono text-xs text-muted-foreground">—</span>;
  }
  if (pnl == null) return <span className="font-mono text-xs text-muted-foreground">—</span>;
  // Format as ``+$300.00`` / ``-$100.00`` — sign outside the $ so the
  // currency reads naturally rather than ``$-100.00``.
  const sign = pnl >= 0 ? "+" : "-";
  const formatted = `${sign}$${Math.abs(pnl).toFixed(2)}`;
  return <span className={cn("font-mono text-xs font-medium", pnlClass(pnl))}>{formatted}</span>;
}

function outcomeToneClass(outcome: string): string {
  if (outcome === "won") return "won";
  if (outcome === "lost") return "lost";
  if (outcome === "cancelled" || outcome === "push") return "cancelled";
  if (outcome === "unresolved") return "unresolved";
  return "pending";
}

function legSummary(leg: PaperParlayRead["legs"][number]): string {
  if (leg.subject_name && leg.stat_key && leg.threshold != null) {
    return `${leg.subject_name} ${leg.threshold}+ ${leg.stat_key.replace(/_/g, " ")}`;
  }
  return leg.market_title || leg.ticker;
}

/**
 * Two-click delete affordance: first click swaps the trash icon for
 * a "Sure?" pill; second click commits the DELETE. Click outside or
 * wait ~3s and it resets. Stops accidental deletions without forcing
 * a full modal for a small action.
 */
function DeleteButton({ parlayId }: { parlayId: number }) {
  const [armed, setArmed] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function handleClick(e: React.MouseEvent<HTMLButtonElement>) {
    e.stopPropagation(); // Don't toggle the expand panel.
    if (!armed) {
      setArmed(true);
      // Auto-disarm after 3s so the trash icon reappears.
      setTimeout(() => setArmed(false), 3000);
      return;
    }
    setDeleting(true);
    try {
      await deletePaperParlay(parlayId);
      await mutate(keys.positions);
    } catch {
      // SWR re-fetch will show the row again if the delete failed.
      setArmed(false);
    } finally {
      setDeleting(false);
    }
  }

  if (armed) {
    return (
      <button
        type="button"
        onClick={handleClick}
        disabled={deleting}
        data-testid={`paper-parlay-confirm-delete-${parlayId}`}
        className="rounded-md border border-negative/40 bg-negative/10 px-2 py-0.5 text-2xs font-medium text-negative focus-visible:ring-focus hover:bg-negative/20"
      >
        {deleting ? "…" : "sure?"}
      </button>
    );
  }
  return (
    <button
      type="button"
      onClick={handleClick}
      aria-label="Delete parlay"
      data-testid={`paper-parlay-delete-${parlayId}`}
      className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-surface-hover hover:text-negative focus-visible:ring-focus"
    >
      <Trash2 size={12} />
    </button>
  );
}
