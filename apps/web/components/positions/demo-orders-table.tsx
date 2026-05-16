"use client";

import useSWR, { mutate } from "swr";
import { fetchPositions, cancelDemoOrder, keys } from "@/lib/api";
import type { PositionsRead, DemoOrderRead } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
import { MarketDetailSheet } from "@/components/markets/market-detail-sheet";
import { TruncationHint } from "@/components/positions/truncation-hint";
import { fmtDatetime, sideClass } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { useState } from "react";
import { usePriceDisplay } from "@/lib/price-display";

function statusPillClass(status: string): string {
  if (status === "filled") return "won";
  if (status === "cancelled") return "cancelled";
  if (status === "rejected") return "lost";
  if (status === "submission_failed") return "lost";
  if (status === "pending") return "pending";
  if (status === "resting") return "pending";
  // Bug #31 — submit + cancel both transit through an intermediate
  // state while the outbox drain processes the Kalshi side effect.
  if (status === "submitting") return "pending";
  if (status === "cancelling") return "pending";
  return "";
}

function approvalPillClass(approved: boolean): string {
  return approved ? "settled" : "pending";
}

function DemoOrderRow({
  order,
  onViewMarket,
  onCancel,
}: {
  order: DemoOrderRead;
  onViewMarket: () => void;
  onCancel: () => void;
}) {
  const { formatPrice } = usePriceDisplay();
  const canCancel = order.status === "pending" || order.status === "resting";

  return (
    <TableRow>
      <TableCell>
        <button
          className="cursor-pointer font-mono text-xs text-accent hover:underline"
          onClick={onViewMarket}
        >
          {order.ticker}
        </button>
      </TableCell>
      <TableCell>
        <span className={cn("font-mono text-xs font-medium", sideClass(order.side))}>
          {order.side.toUpperCase()}
        </span>
      </TableCell>
      <TableCell>
        <Badge variant="outline" className="text-xs">
          {order.action}
        </Badge>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{order.quantity}</span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{formatPrice(order.limit_price)}</span>
      </TableCell>
      <TableCell>
        <span className={cn("outcome-pill", statusPillClass(order.status))}>
          {order.status}
        </span>
      </TableCell>
      <TableCell>
        <span className={cn("outcome-pill", approvalPillClass(order.approved_by_user))}>
          {order.approved_by_user ? "Approved" : "Pending"}
        </span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs text-muted-foreground">
          {fmtDatetime(order.submitted_at)}
        </span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs text-muted-foreground">
          {order.kalshi_order_id ?? "—"}
        </span>
      </TableCell>
      <TableCell>
        {canCancel && (
          <Button variant="danger" size="xs" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </TableCell>
    </TableRow>
  );
}

function DemoOrderCard({
  order,
  onViewMarket,
  onCancel,
}: {
  order: DemoOrderRead;
  onViewMarket: () => void;
  onCancel: () => void;
}) {
  const { formatPrice } = usePriceDisplay();
  const canCancel = order.status === "pending" || order.status === "resting";

  return (
    <article className="pred-card">
      <div className="pred-card-head">
        <button
          className="min-w-0 cursor-pointer text-left"
          onClick={onViewMarket}
        >
          <p className="truncate font-mono text-xs text-accent hover:underline">
            {order.ticker}
          </p>
          <p className="pred-card-time mt-1">
            Submitted {fmtDatetime(order.submitted_at)}
          </p>
        </button>
        <div className="flex flex-col items-end gap-2">
          <span className={cn("outcome-pill", statusPillClass(order.status))}>
            {order.status}
          </span>
          <span className={cn("outcome-pill", approvalPillClass(order.approved_by_user))}>
            {order.approved_by_user ? "Approved" : "Pending"}
          </span>
        </div>
      </div>

      <div className="pred-card-grid">
        <div>
          <p className="pred-card-stat-label">Side</p>
          <p className={cn("pred-card-stat-value", sideClass(order.side))}>
            {order.side.toUpperCase()}
          </p>
        </div>
        <div>
          <p className="pred-card-stat-label">Action</p>
          <p className="pred-card-stat-value">{order.action}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">Qty</p>
          <p className="pred-card-stat-value">{order.quantity}</p>
        </div>
        <div>
          <p className="pred-card-stat-label">Limit</p>
          <p className="pred-card-stat-value">{formatPrice(order.limit_price)}</p>
        </div>
        <div className="col-span-2">
          <p className="pred-card-stat-label">Kalshi ID</p>
          <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
            {order.kalshi_order_id ?? "—"}
          </p>
        </div>
      </div>

      {canCancel && (
        <Button variant="danger" size="sm" onClick={onCancel} className="w-full">
          Cancel Order
        </Button>
      )}
    </article>
  );
}

interface DemoOrdersTableProps {
  maxHeight?: string;
}

export function DemoOrdersTable({ maxHeight }: DemoOrdersTableProps) {
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);

  const { data, isLoading, error } = useSWR<PositionsRead>(
    keys.positions,
    fetchPositions,
    { refreshInterval: 15_000 },
  );

  const orders = data?.demo_orders ?? [];
  const truncated = data?.demo_truncated === true;
  async function handleCancel(id: number) {
    try {
      await cancelDemoOrder(id);
      await mutate(keys.positions);
    } catch {
      /* ignore */
    }
  }

  if (error) {
    return (
      <div className="flex h-24 items-center justify-center text-xs text-negative">
        Failed to load demo orders.
      </div>
    );
  }

  return (
    <>
      {truncated ? (
        <TruncationHint visibleCount={orders.length} limitParam="demo_limit" />
      ) : null}
      <div className="space-y-3 lg:hidden">
        {isLoading
          ? Array.from({ length: 4 }).map((_, index) => (
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
          : orders.length === 0
            ? (
              <div className="cosmos-table-empty">
                No demo orders yet
              </div>
            )
            : orders.map((order) => (
                <DemoOrderCard
                  key={order.id}
                  order={order}
                  onViewMarket={() => setSelectedTicker(order.ticker)}
                  onCancel={() => handleCancel(order.id)}
                />
              ))}
      </div>

      <div className="hidden lg:block">
        <div
          className="cosmos-table-wrap"
          style={maxHeight ? { maxHeight, overflow: "auto" } : undefined}
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Ticker</TableHead>
                <TableHead className="w-14">Side</TableHead>
                <TableHead className="w-20">Action</TableHead>
                <TableHead className="w-16">Qty</TableHead>
                <TableHead className="w-20">Limit</TableHead>
                <TableHead className="w-24">Status</TableHead>
                <TableHead className="w-24">Approval</TableHead>
                <TableHead className="w-36">Submitted</TableHead>
                <TableHead className="w-32">Kalshi ID</TableHead>
                <TableHead className="w-16" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading
                ? Array.from({ length: 5 }).map((_, index) => <SkeletonRow key={index} cols={10} />)
                : orders.length === 0
                  ? (
                    <TableRow>
                      <TableCell colSpan={10} className="cosmos-table-empty">
                        No demo orders yet
                      </TableCell>
                    </TableRow>
                  )
                  : orders.map((order) => (
                      <DemoOrderRow
                        key={order.id}
                        order={order}
                        onViewMarket={() => setSelectedTicker(order.ticker)}
                        onCancel={() => handleCancel(order.id)}
                      />
                    ))}
            </TableBody>
          </Table>
        </div>
      </div>

      <MarketDetailSheet
        ticker={selectedTicker}
        onClose={() => setSelectedTicker(null)}
      />
    </>
  );
}
