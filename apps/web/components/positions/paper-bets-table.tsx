"use client";

import { useMemo, useState } from "react";
import useSWR, { mutate } from "swr";
import {
  ChevronDown,
  ChevronRight,
  Layers,
  Target,
  Trash2,
} from "lucide-react";
import {
  deletePaperParlay,
  deletePaperPosition,
  exitPaperPosition,
  fetchPositions,
  keys,
} from "@/lib/api";
import type {
  PaperParlayRead,
  PaperPositionRead,
  PositionsRead,
} from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
import { TruncationHint } from "@/components/positions/truncation-hint";
import { MarketDetailSheet } from "@/components/markets/market-detail-sheet";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn, fmtDatetime, pnlClass, sideClass } from "@/lib/utils";
import { usePriceDisplay } from "@/lib/price-display";

/**
 * Unified Paper Bets table — singles and parlays merged into one
 * sorted-by-recency feed. Parlay rows expand to reveal legs. Each
 * row shows the stake the operator put in, the projected payout if
 * it hits, and the realized PnL once settled.
 *
 * Replaces the separate PaperPositionsTable + PaperParlaysTable on
 * the portfolio page. Demo orders are gone entirely (the feature
 * was retired in an earlier phase). Settled and open bets sit in
 * the same feed because the operator doesn't think of them as
 * separate inventories — they think "what trades have I made?".
 */
export function PaperBetsTable() {
  const { data, isLoading, error } = useSWR<PositionsRead>(
    keys.positions,
    fetchPositions,
    { refreshInterval: 15_000 },
  );

  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [expandedParlayId, setExpandedParlayId] = useState<number | null>(null);
  const [exitingPosition, setExitingPosition] = useState<PaperPositionRead | null>(null);

  const bets = useMemo(
    () => mergeBets(data?.paper_positions ?? [], data?.paper_parlays ?? []),
    [data?.paper_positions, data?.paper_parlays],
  );
  const truncated =
    (data?.paper_truncated === true) || (data?.paper_parlays_truncated === true);

  // Only hard-fail when there's no cached data. SWR keeps the last good
  // response on a failed revalidation (stale-if-error); blanking the whole
  // table on a transient 15s poll timeout hid the operator's open exposure.
  if (error && !data) {
    return (
      <div className="flex h-24 items-center justify-center text-xs text-negative">
        Failed to load paper bets.
      </div>
    );
  }

  const empty = !isLoading && bets.length === 0;

  return (
    <>
      {truncated ? <TruncationHint visibleCount={bets.length} limitParam="paper_limit" /> : null}

      {/* Mobile: cards */}
      <div className="space-y-3 lg:hidden">
        {isLoading ? (
          Array.from({ length: 3 }).map((_, index) => (
            <article key={index} className="pred-card">
              <Skeleton className="h-4 w-40" />
              <div className="pred-card-grid">
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
              </div>
            </article>
          ))
        ) : empty ? (
          <div className="cosmos-table-empty" data-testid="paper-bets-empty">
            No paper bets yet
          </div>
        ) : (
          bets.map((bet) =>
            bet.kind === "single" ? (
              <SinglePositionCard
                key={`single-${bet.data.id}`}
                position={bet.data}
                onViewMarket={() => setSelectedTicker(bet.data.ticker)}
                onExit={() => setExitingPosition(bet.data)}
              />
            ) : (
              <ParlayCard key={`parlay-${bet.data.id}`} parlay={bet.data} />
            ),
          )
        )}
      </div>

      {/* Desktop: unified table */}
      <div className="hidden lg:block">
        <div className="cosmos-table-wrap">
          <Table data-testid="paper-bets-table">
            <TableHeader>
              <TableRow>
                <TableHead className="w-10" />
                <TableHead className="w-20">Type</TableHead>
                <TableHead>Description</TableHead>
                <TableHead className="w-24 text-right">Stake</TableHead>
                <TableHead className="w-24">Status</TableHead>
                <TableHead className="w-28 text-right">Projected</TableHead>
                <TableHead className="w-24 text-right">PnL</TableHead>
                <TableHead className="w-36">Opened</TableHead>
                <TableHead className="w-24" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                Array.from({ length: 5 }).map((_, index) => <SkeletonRow key={index} cols={9} />)
              ) : empty ? (
                <TableRow>
                  <TableCell colSpan={9} className="cosmos-table-empty">
                    No paper bets yet
                  </TableCell>
                </TableRow>
              ) : (
                bets.map((bet) =>
                  bet.kind === "single" ? (
                    <SinglePositionRow
                      key={`single-${bet.data.id}`}
                      position={bet.data}
                      onViewMarket={() => setSelectedTicker(bet.data.ticker)}
                      onExit={() => setExitingPosition(bet.data)}
                    />
                  ) : (
                    <ParlayRowGroup
                      key={`parlay-${bet.data.id}`}
                      parlay={bet.data}
                      expanded={expandedParlayId === bet.data.id}
                      onToggle={() =>
                        setExpandedParlayId((current) =>
                          current === bet.data.id ? null : bet.data.id,
                        )
                      }
                    />
                  ),
                )
              )}
            </TableBody>
          </Table>
        </div>
      </div>

      <MarketDetailSheet ticker={selectedTicker} onClose={() => setSelectedTicker(null)} />
      {exitingPosition && (
        <ExitPositionDialog
          position={exitingPosition}
          onClose={() => setExitingPosition(null)}
        />
      )}
    </>
  );
}

// =============================================================================
// Row + card components
// =============================================================================

interface SingleRowProps {
  position: PaperPositionRead;
  onViewMarket: () => void;
  onExit: () => void;
}

function SinglePositionRow({ position, onViewMarket, onExit }: SingleRowProps) {
  const { formatPrice } = usePriceDisplay();
  const stake = position.quantity * position.entry_price;
  // YES contract pays $1 if YES wins; NO contract pays $1 if NO wins.
  // Either way, the projected payout = quantity (number of contracts).
  const projected = position.quantity;
  const isOpen = position.status === "open";

  return (
    <TableRow data-testid={`paper-bet-single-${position.id}`}>
      <TableCell />
      <TableCell>
        <TypeBadge kind="single" />
      </TableCell>
      <TableCell>
        <button
          className="cursor-pointer text-left font-mono text-xs text-accent hover:underline focus-visible:ring-focus"
          onClick={onViewMarket}
        >
          <span className="block truncate">{position.ticker}</span>
        </button>
        <span className={cn("mt-0.5 inline-block font-mono text-2xs", sideClass(position.side))}>
          {position.side.toUpperCase()} @ {formatPrice(position.entry_price)}
        </span>
      </TableCell>
      <TableCell className="text-right font-mono text-xs">${stake.toFixed(2)}</TableCell>
      <TableCell>
        <span className={cn("outcome-pill", singleStatusClass(position.status))}>
          {position.status}
        </span>
      </TableCell>
      <TableCell className="text-right font-mono text-xs">
        {isOpen ? `$${projected.toFixed(2)}` : "—"}
      </TableCell>
      <TableCell className="text-right">
        <PnlCell pnl={position.pnl ?? null} pending={isOpen} />
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {fmtDatetime(position.opened_at)}
      </TableCell>
      <TableCell>
        <div className="flex items-center justify-end gap-1.5">
          {isOpen && (
            <Button variant="danger" size="xs" onClick={onExit}>
              Exit
            </Button>
          )}
          <DeleteButton
            testIdPrefix={`paper-bet-single-${position.id}`}
            onDelete={async () => {
              await deletePaperPosition(position.id);
              await mutate(keys.positions);
            }}
            label="Delete paper position"
          />
        </div>
      </TableCell>
    </TableRow>
  );
}

interface ParlayRowGroupProps {
  parlay: PaperParlayRead;
  expanded: boolean;
  onToggle: () => void;
}

function ParlayRowGroup({ parlay, expanded, onToggle }: ParlayRowGroupProps) {
  const isOpen = parlay.outcome === "pending" || parlay.outcome === "unresolved";
  const projected =
    parlay.combined_market_price > 0 ? parlay.stake / parlay.combined_market_price : null;
  const ChevronIcon = expanded ? ChevronDown : ChevronRight;

  return (
    <>
      <TableRow
        data-testid={`paper-bet-parlay-${parlay.id}`}
        className="cursor-pointer hover:bg-surface-hover/40"
        onClick={onToggle}
      >
        <TableCell className="w-10 align-middle">
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onToggle();
            }}
            aria-label={expanded ? "Collapse legs" : "Expand legs"}
            aria-expanded={expanded}
            className="flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:bg-surface-hover hover:text-foreground focus-visible:ring-focus"
          >
            <ChevronIcon size={14} />
          </button>
        </TableCell>
        <TableCell>
          <TypeBadge kind="parlay" />
        </TableCell>
        <TableCell>
          <span className="text-xs text-foreground">
            {parlay.leg_count}-leg {parlay.sport_scope || "parlay"}
          </span>
          <span className="mt-0.5 block font-mono text-2xs text-muted-foreground">
            combined {parlay.combined_market_price.toFixed(2)} · {parlay.american_odds}
          </span>
        </TableCell>
        <TableCell className="text-right font-mono text-xs">${parlay.stake.toFixed(2)}</TableCell>
        <TableCell>
          <span className={cn("outcome-pill", parlayOutcomeClass(parlay.outcome))}>
            {parlay.outcome}
          </span>
        </TableCell>
        <TableCell className="text-right font-mono text-xs">
          {isOpen && projected != null ? `$${projected.toFixed(2)}` : "—"}
        </TableCell>
        <TableCell className="text-right">
          <PnlCell pnl={parlay.realized_pnl ?? null} pending={isOpen} />
        </TableCell>
        <TableCell className="font-mono text-xs text-muted-foreground">
          {fmtDatetime(parlay.created_at)}
        </TableCell>
        <TableCell>
          <div className="flex items-center justify-end">
            <DeleteButton
              testIdPrefix={`paper-bet-parlay-${parlay.id}`}
              onDelete={async () => {
                await deletePaperParlay(parlay.id);
                await mutate(keys.positions);
              }}
              label="Delete paper parlay"
              stopPropagation
            />
          </div>
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow data-testid={`paper-bet-parlay-detail-${parlay.id}`}>
          <TableCell colSpan={9} className="bg-surface/40 px-6 py-4">
            <ParlayDetail parlay={parlay} />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function SinglePositionCard({ position, onViewMarket, onExit }: SingleRowProps) {
  const { formatPrice } = usePriceDisplay();
  const stake = position.quantity * position.entry_price;
  const projected = position.quantity;
  const isOpen = position.status === "open";

  return (
    <article className="pred-card">
      <div className="pred-card-head">
        <button className="min-w-0 cursor-pointer text-left focus-visible:ring-focus" onClick={onViewMarket}>
          <div className="flex items-center gap-1.5">
            <TypeBadge kind="single" />
            <p className="truncate font-mono text-xs text-accent hover:underline">{position.ticker}</p>
          </div>
          <p className="pred-card-time mt-1">Opened {fmtDatetime(position.opened_at)}</p>
        </button>
        <span className={cn("outcome-pill", singleStatusClass(position.status))}>{position.status}</span>
      </div>

      <div className="pred-card-grid">
        <div>
          <p className="pred-card-stat-label">Stake</p>
          <p className="pred-card-stat-value">${stake.toFixed(2)}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">{position.side.toUpperCase()} @ {formatPrice(position.entry_price)}</p>
          <p className={cn("pred-card-stat-value", sideClass(position.side))}>{position.quantity} ct</p>
        </div>
        <div>
          <p className="pred-card-stat-label">Projected</p>
          <p className="pred-card-stat-value">{isOpen ? `$${projected.toFixed(2)}` : "—"}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">PnL</p>
          <div className="mt-1">
            <PnlCell pnl={position.pnl ?? null} pending={isOpen} />
          </div>
        </div>
      </div>

      {isOpen && (
        <Button variant="danger" size="sm" onClick={onExit} className="w-full">
          Exit Position
        </Button>
      )}
    </article>
  );
}

function ParlayCard({ parlay }: { parlay: PaperParlayRead }) {
  const [expanded, setExpanded] = useState(false);
  const isOpen = parlay.outcome === "pending" || parlay.outcome === "unresolved";
  const projected =
    parlay.combined_market_price > 0 ? parlay.stake / parlay.combined_market_price : null;
  const ChevronIcon = expanded ? ChevronDown : ChevronRight;

  return (
    <article className="pred-card" data-testid={`paper-bet-parlay-card-${parlay.id}`}>
      <div className="pred-card-head">
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="min-w-0 cursor-pointer text-left focus-visible:ring-focus"
        >
          <div className="flex items-center gap-1.5">
            <ChevronIcon size={14} className="text-muted-foreground" />
            <TypeBadge kind="parlay" />
            <p className="font-mono text-xs text-foreground">
              {parlay.leg_count}-leg parlay
            </p>
          </div>
          <p className="pred-card-time mt-1">Created {fmtDatetime(parlay.created_at)}</p>
        </button>
        <span className={cn("outcome-pill", parlayOutcomeClass(parlay.outcome))}>{parlay.outcome}</span>
      </div>

      <div className="pred-card-grid">
        <div>
          <p className="pred-card-stat-label">Stake</p>
          <p className="pred-card-stat-value">${parlay.stake.toFixed(2)}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">Combined / Odds</p>
          <p className="pred-card-stat-value">{parlay.combined_market_price.toFixed(2)} · {parlay.american_odds}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">Projected</p>
          <p className="pred-card-stat-value">{isOpen && projected != null ? `$${projected.toFixed(2)}` : "—"}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">PnL</p>
          <div className="mt-1">
            <PnlCell pnl={parlay.realized_pnl ?? null} pending={isOpen} />
          </div>
        </div>
      </div>

      {expanded && (
        <div className="mt-3 border-t border-border/40 pt-3">
          <ParlayDetail parlay={parlay} />
        </div>
      )}
    </article>
  );
}

function ParlayDetail({ parlay }: { parlay: PaperParlayRead }) {
  return (
    <div className="grid gap-3">
      {parlay.notes && (
        <p className="text-xs italic text-muted-foreground">&ldquo;{parlay.notes}&rdquo;</p>
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

function legSummary(leg: PaperParlayRead["legs"][number]): string {
  if (leg.subject_name && leg.stat_key && leg.threshold != null) {
    return `${leg.subject_name} ${leg.threshold}+ ${leg.stat_key.replace(/_/g, " ")}`;
  }
  return leg.market_title || leg.ticker;
}

// =============================================================================
// Small reusable presentational helpers
// =============================================================================

function TypeBadge({ kind }: { kind: "single" | "parlay" }) {
  const Icon = kind === "single" ? Target : Layers;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-2xs font-medium",
        kind === "single"
          ? "border-accent/30 bg-accent/5 text-accent"
          : "border-warning/30 bg-warning/8 text-warning",
      )}
    >
      <Icon size={10} />
      {kind}
    </span>
  );
}

function PnlCell({ pnl, pending }: { pnl: number | null; pending: boolean }) {
  if (pending) return <span className="font-mono text-xs text-muted-foreground">Open</span>;
  if (pnl == null) return <span className="font-mono text-xs text-muted-foreground">—</span>;
  const sign = pnl >= 0 ? "+" : "-";
  return (
    <span className={cn("font-mono text-xs font-medium", pnlClass(pnl))}>
      {sign}${Math.abs(pnl).toFixed(2)}
    </span>
  );
}

function singleStatusClass(status: string): string {
  if (status === "open") return "pending";
  if (status === "closed") return "settled";
  return "";
}

function parlayOutcomeClass(outcome: string): string {
  if (outcome === "won") return "won";
  if (outcome === "lost") return "lost";
  if (outcome === "cancelled" || outcome === "push") return "cancelled";
  if (outcome === "unresolved") return "unresolved";
  return "pending";
}

// =============================================================================
// Delete button (two-click confirm — matches the older tables)
// =============================================================================

function DeleteButton({
  onDelete,
  label,
  testIdPrefix,
  stopPropagation = false,
}: {
  onDelete: () => Promise<void>;
  label: string;
  testIdPrefix: string;
  stopPropagation?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function handleClick(event: React.MouseEvent<HTMLButtonElement>) {
    if (stopPropagation) event.stopPropagation();
    if (!armed) {
      setArmed(true);
      setTimeout(() => setArmed(false), 5000);
      return;
    }
    setDeleting(true);
    try {
      await onDelete();
    } catch {
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
        data-testid={`${testIdPrefix}-confirm-delete`}
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
      aria-label={label}
      data-testid={`${testIdPrefix}-delete`}
      className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-surface-hover hover:text-negative focus-visible:ring-focus"
    >
      <Trash2 size={12} />
    </button>
  );
}

// =============================================================================
// Exit dialog — opens for an open single position
// =============================================================================

function ExitPositionDialog({
  position,
  onClose,
}: {
  position: PaperPositionRead;
  onClose: () => void;
}) {
  const { mode, formatEditablePrice, formatPrice, parsePriceInput } = usePriceDisplay();
  const [exitPrice, setExitPrice] = useState(formatEditablePrice(position.entry_price));
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  async function handleExit() {
    const price = parsePriceInput(exitPrice);
    if (price == null || price <= 0 || price >= 1) {
      setErrorMessage("Enter a valid exit price.");
      return;
    }
    setLoading(true);
    setErrorMessage(null);
    try {
      await exitPaperPosition(position.id, { exit_price: price });
      await mutate(keys.positions);
      onClose();
    } catch (caughtError) {
      setErrorMessage(
        caughtError instanceof Error ? caughtError.message : "Failed to exit position",
      );
    } finally {
      setLoading(false);
    }
  }

  const previewExitPrice = parsePriceInput(exitPrice);
  const previewPnl =
    previewExitPrice == null
      ? null
      : (previewExitPrice - position.entry_price) * position.quantity;

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Exit Position</DialogTitle>
          <DialogDescription>
            {position.ticker} · {position.side.toUpperCase()} · {position.quantity} contracts
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-3">
          <div>
            <label className="mb-1.5 block text-xs text-muted-foreground">
              {mode === "american"
                ? "Exit price (American odds)"
                : mode === "prediction"
                  ? "Exit price (prediction %)"
                  : "Exit price (Kalshi cents)"}
            </label>
            <Input
              mono
              value={exitPrice}
              onChange={(event) => setExitPrice(event.target.value)}
              placeholder={mode === "american" ? "-110" : mode === "prediction" ? "54.0" : "55"}
            />
          </div>
          {previewExitPrice != null && (
            <div className="text-xs text-muted-foreground">
              Entry: {formatPrice(position.entry_price)} → Exit: {formatPrice(previewExitPrice)} ·
              PnL: <span className={pnlClass(previewPnl)}>{previewPnl != null ? `${previewPnl >= 0 ? "+" : ""}$${previewPnl.toFixed(2)}` : "—"}</span>
            </div>
          )}
          {errorMessage && <p className="text-xs text-negative">{errorMessage}</p>}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="danger" size="sm" onClick={handleExit} disabled={loading}>
            {loading ? "Exiting..." : "Exit Position"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// =============================================================================
// Merge helper — singles and parlays into one chronological feed
// =============================================================================

type PaperBet =
  | { kind: "single"; data: PaperPositionRead; sortDate: string }
  | { kind: "parlay"; data: PaperParlayRead; sortDate: string };

function mergeBets(
  positions: PaperPositionRead[],
  parlays: PaperParlayRead[],
): PaperBet[] {
  const out: PaperBet[] = [
    ...positions.map(
      (p): PaperBet => ({ kind: "single", data: p, sortDate: p.opened_at }),
    ),
    ...parlays.map(
      (p): PaperBet => ({ kind: "parlay", data: p, sortDate: p.created_at }),
    ),
  ];
  // Descending — newest at top.
  out.sort((a, b) => (a.sortDate < b.sortDate ? 1 : a.sortDate > b.sortDate ? -1 : 0));
  return out;
}
