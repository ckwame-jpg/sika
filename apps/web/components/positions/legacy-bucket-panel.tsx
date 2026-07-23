"use client";

import { useState } from "react";
import useSWR from "swr";
import { ChevronDown, ChevronRight } from "lucide-react";
import { fetchPositions, keys } from "@/lib/api";
import type {
  DemoOrderRead,
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
import { fmtDatetime, pnlClass } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { TruncationHint } from "@/components/positions/truncation-hint";

/**
 * Multi-user batch follow-up — read-only renderer for the legacy
 * bucket on /positions.
 *
 * The /positions aggregator (PR 3) splits per-user data into the
 * primary fields and historical / pre-multi-user data into the
 * ``legacy_*`` fields. The existing PaperPositionsTable, DemoOrdersTable,
 * and PaperParlaysTable read the primary fields and own their own
 * "exit" / "cancel" affordances; legacy rows can't be exited (no
 * clear owner), so this component renders all three legacy lists in
 * one collapsible section with NO action buttons.
 *
 * Hidden entirely when every legacy list is empty — operators who
 * never had pre-multi-user data don't see an empty container.
 */

export function LegacyBucketPanel() {
  const { data, isLoading } = useSWR<PositionsRead>(keys.positions, () => fetchPositions(), {
    revalidateOnFocus: false,
  });
  const [expanded, setExpanded] = useState(true);

  if (isLoading || !data) return null;

  const paperPositions = data.legacy_paper_positions ?? [];
  const demoOrders = data.legacy_demo_orders ?? [];
  const paperParlays = data.legacy_paper_parlays ?? [];
  const totalRows = paperPositions.length + demoOrders.length + paperParlays.length;

  if (totalRows === 0) return null;

  const ChevronIcon = expanded ? ChevronDown : ChevronRight;

  return (
    <section
      className="cosmos-panel"
      data-testid="legacy-bucket-panel"
    >
      <div className="cosmos-panel-head">
        <button
          type="button"
          className="flex flex-1 items-start gap-2 text-left focus-visible:ring-focus"
          onClick={() => setExpanded((prev) => !prev)}
          aria-expanded={expanded}
          data-testid="legacy-bucket-toggle"
        >
          <ChevronIcon
            size={14}
            className="mt-0.5 shrink-0 text-muted-foreground"
            aria-hidden
          />
          <div className="cosmos-panel-head-text">
            <h2 className="cosmos-panel-title">
              Legacy (pre-multi-user) <span className="text-muted-foreground font-normal text-sm">· {totalRows}</span>
            </h2>
            <p className="cosmos-panel-desc">
              Historical paper trades + demo orders created before per-user
              scoping landed. Read-only for everyone — no clear owner to
              attribute exits to.
            </p>
          </div>
        </button>
      </div>
      {expanded && (
        <div className="cosmos-panel-body flush space-y-6 pt-2">
          {paperPositions.length > 0 && (
            <LegacyPaperPositions
              positions={paperPositions}
              truncated={data.legacy_paper_truncated}
            />
          )}
          {demoOrders.length > 0 && (
            <LegacyDemoOrders
              orders={demoOrders}
              truncated={data.legacy_demo_truncated}
            />
          )}
          {paperParlays.length > 0 && (
            <LegacyPaperParlays
              parlays={paperParlays}
              truncated={data.legacy_paper_parlays_truncated}
            />
          )}
        </div>
      )}
    </section>
  );
}

function SectionHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="px-4 pb-2 pt-1">
      <h3 className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
        {title} <span className="ml-1 text-foreground/70 tracking-normal normal-case">· {count}</span>
      </h3>
    </div>
  );
}

function LegacyPaperPositions({
  positions,
  truncated,
}: {
  positions: PaperPositionRead[];
  truncated: boolean;
}) {
  return (
    <div data-testid="legacy-paper-positions">
      <SectionHeader title="Paper positions" count={positions.length} />
      {truncated ? (
        <TruncationHint visibleCount={positions.length} limitParam="paper_limit" />
      ) : null}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Ticker</TableHead>
            <TableHead>Side</TableHead>
            <TableHead className="text-right">Qty</TableHead>
            <TableHead className="text-right">Entry</TableHead>
            <TableHead className="text-right">Exit</TableHead>
            <TableHead className="text-right">PnL</TableHead>
            <TableHead className="text-right">Status</TableHead>
            <TableHead className="text-right">Opened</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {positions.map((position) => (
            <TableRow key={position.id} data-testid={`legacy-paper-position-${position.id}`}>
              <TableCell className="font-mono text-xs">{position.ticker}</TableCell>
              <TableCell className="text-xs uppercase">{position.side}</TableCell>
              <TableCell className="text-right font-mono text-xs">{position.quantity}</TableCell>
              <TableCell className="text-right font-mono text-xs">{position.entry_price.toFixed(2)}</TableCell>
              <TableCell className="text-right font-mono text-xs">
                {position.exit_price != null ? position.exit_price.toFixed(2) : "—"}
              </TableCell>
              <TableCell className="text-right">
                <PnlCell pnl={position.pnl} status={position.status} />
              </TableCell>
              <TableCell className="text-right text-xs">{position.status}</TableCell>
              <TableCell className="text-right font-mono text-xs">
                {fmtDatetime(position.opened_at)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function LegacyDemoOrders({
  orders,
  truncated,
}: {
  orders: DemoOrderRead[];
  truncated: boolean;
}) {
  return (
    <div data-testid="legacy-demo-orders">
      <SectionHeader title="Demo orders" count={orders.length} />
      {truncated ? (
        <TruncationHint visibleCount={orders.length} limitParam="demo_limit" />
      ) : null}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Ticker</TableHead>
            <TableHead>Side</TableHead>
            <TableHead>Action</TableHead>
            <TableHead className="text-right">Qty</TableHead>
            <TableHead className="text-right">Limit</TableHead>
            <TableHead className="text-right">Status</TableHead>
            <TableHead className="text-right">Submitted</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {orders.map((order) => (
            <TableRow key={order.id} data-testid={`legacy-demo-order-${order.id}`}>
              <TableCell className="font-mono text-xs">{order.ticker}</TableCell>
              <TableCell className="text-xs uppercase">{order.side}</TableCell>
              <TableCell className="text-xs">{order.action}</TableCell>
              <TableCell className="text-right font-mono text-xs">{order.quantity}</TableCell>
              <TableCell className="text-right font-mono text-xs">{order.limit_price.toFixed(2)}</TableCell>
              <TableCell className="text-right text-xs">{order.status}</TableCell>
              <TableCell className="text-right font-mono text-xs">
                {order.submitted_at ? fmtDatetime(order.submitted_at) : "—"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function LegacyPaperParlays({
  parlays,
  truncated,
}: {
  parlays: PaperParlayRead[];
  truncated: boolean;
}) {
  return (
    <div data-testid="legacy-paper-parlays">
      <SectionHeader title="Paper parlays" count={parlays.length} />
      {truncated ? (
        <TruncationHint visibleCount={parlays.length} limitParam="paper_limit" />
      ) : null}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Created</TableHead>
            <TableHead>Sport</TableHead>
            <TableHead className="text-right">Legs</TableHead>
            <TableHead className="text-right">Combined</TableHead>
            <TableHead className="text-right">Odds</TableHead>
            <TableHead className="text-right">Stake</TableHead>
            <TableHead className="text-right">Status</TableHead>
            <TableHead className="text-right">PnL</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {parlays.map((parlay) => (
            <TableRow key={parlay.id} data-testid={`legacy-paper-parlay-${parlay.id}`}>
              <TableCell className="font-mono text-xs">{fmtDatetime(parlay.created_at)}</TableCell>
              <TableCell className="text-xs">{parlay.sport_scope}</TableCell>
              <TableCell className="text-right font-mono text-xs">{parlay.leg_count}</TableCell>
              <TableCell className="text-right font-mono text-xs">{parlay.combined_market_price.toFixed(2)}</TableCell>
              <TableCell className="text-right font-mono text-xs">{parlay.american_odds}</TableCell>
              <TableCell className="text-right font-mono text-xs">${parlay.stake.toFixed(2)}</TableCell>
              <TableCell className="text-right text-xs">{parlay.outcome}</TableCell>
              <TableCell className="text-right">
                <ParlayPnlCell pnl={parlay.realized_pnl} outcome={parlay.outcome} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function PnlCell({ pnl, status }: { pnl: number | null; status: string }) {
  if (status === "open") return <span className="font-mono text-xs text-muted-foreground">Open</span>;
  if (pnl == null) return <span className="font-mono text-xs text-muted-foreground">—</span>;
  const sign = pnl >= 0 ? "+" : "-";
  return (
    <span className={cn("font-mono text-xs font-medium", pnlClass(pnl))}>
      {sign}${Math.abs(pnl).toFixed(2)}
    </span>
  );
}

function ParlayPnlCell({ pnl, outcome }: { pnl: number | null; outcome: string }) {
  if (outcome === "pending" || outcome === "unresolved") {
    return <span className="font-mono text-xs text-muted-foreground">—</span>;
  }
  if (pnl == null) return <span className="font-mono text-xs text-muted-foreground">—</span>;
  const sign = pnl >= 0 ? "+" : "-";
  return (
    <span className={cn("font-mono text-xs font-medium", pnlClass(pnl))}>
      {sign}${Math.abs(pnl).toFixed(2)}
    </span>
  );
}
