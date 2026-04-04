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
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
import { MarketDetailSheet } from "@/components/markets/market-detail-sheet";
import { fmtDatetime, sideClass } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { useState } from "react";
import { usePriceDisplay } from "@/lib/price-display";

function statusVariant(status: string): "positive" | "negative" | "warning" | "default" {
  if (status === "filled") return "positive";
  if (status === "cancelled" || status === "rejected") return "negative";
  if (status === "pending") return "warning";
  return "default";
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
        <Badge variant={statusVariant(order.status)}>
          {order.status}
        </Badge>
      </TableCell>
      <TableCell>
        <Badge variant={order.approved_by_user ? "positive" : "default"}>
          {order.approved_by_user ? "Approved" : "Pending"}
        </Badge>
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
    <Card className="bg-surface-hover shadow-none">
      <CardContent className="space-y-3 px-4 py-4">
        <div className="flex items-start justify-between gap-3">
          <button
            className="min-w-0 cursor-pointer text-left"
            onClick={onViewMarket}
          >
            <p className="truncate font-mono text-xs text-accent hover:underline">
              {order.ticker}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              Submitted {fmtDatetime(order.submitted_at)}
            </p>
          </button>
          <div className="flex flex-col items-end gap-2">
            <Badge variant={statusVariant(order.status)}>{order.status}</Badge>
            <Badge variant={order.approved_by_user ? "positive" : "default"}>
              {order.approved_by_user ? "Approved" : "Pending"}
            </Badge>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 text-xs">
          <div>
            <p className="text-muted-foreground">Side</p>
            <p className={cn("mt-1 font-mono font-medium", sideClass(order.side))}>
              {order.side.toUpperCase()}
            </p>
          </div>
          <div>
            <p className="text-muted-foreground">Action</p>
            <p className="mt-1 text-foreground">{order.action}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Qty</p>
            <p className="mt-1 font-mono text-foreground">{order.quantity}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Limit</p>
            <p className="mt-1 font-mono text-foreground">{formatPrice(order.limit_price)}</p>
          </div>
          <div className="col-span-2">
            <p className="text-muted-foreground">Kalshi ID</p>
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
      </CardContent>
    </Card>
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
      <div className="space-y-3 lg:hidden">
        {isLoading
          ? Array.from({ length: 4 }).map((_, index) => (
              <Card key={index} className="bg-surface-hover shadow-none">
                <CardContent className="space-y-3 px-4 py-4">
                  <Skeleton className="h-4 w-40" />
                  <div className="grid grid-cols-2 gap-3">
                    <Skeleton className="h-10 w-full" />
                    <Skeleton className="h-10 w-full" />
                    <Skeleton className="h-10 w-full" />
                    <Skeleton className="h-10 w-full" />
                  </div>
                </CardContent>
              </Card>
            ))
          : orders.length === 0
            ? (
              <div className="flex h-24 items-center justify-center rounded-xl border border-border bg-surface text-center text-xs text-muted-foreground">
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
        <div className={maxHeight ? "overflow-auto" : "overflow-x-auto"} style={maxHeight ? { maxHeight } : undefined}>
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
                      <TableCell colSpan={10} className="py-8 text-center text-xs text-muted-foreground">
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
