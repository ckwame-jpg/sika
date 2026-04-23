"use client";

import { useState } from "react";
import useSWR from "swr";
import { AdminTokenCard } from "@/components/admin/admin-token-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fetchKalshiAccount, keys } from "@/lib/api";
import { useAdminToken } from "@/lib/admin-token";
import type { KalshiAccountRead, LiveOrderRead } from "@/lib/types";
import { usePriceDisplay } from "@/lib/price-display";
import { fmtDatetime } from "@/lib/utils";

function formatMoney(cents: number | null | undefined) {
  if (cents == null) return "—";
  return `$${(cents / 100).toFixed(2)}`;
}

function statusVariant(status: string): "positive" | "negative" | "warning" | "default" {
  if (["filled", "executed", "completed"].includes(status)) return "positive";
  if (["cancelled", "rejected", "submission_failed"].includes(status)) return "negative";
  if (["submitting", "pending", "resting"].includes(status)) return "warning";
  return "default";
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-border bg-surface-hover px-3 py-2">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 font-mono text-sm text-foreground">{value}</p>
    </div>
  );
}

function rawPositions(data: KalshiAccountRead) {
  const payload = data.snapshot?.payload;
  const positions = payload?.positions;
  return Array.isArray(positions) ? positions.slice(0, 8) : [];
}

function positionText(item: unknown, key: string) {
  if (!item || typeof item !== "object") return "—";
  const record = item as Record<string, unknown>;
  const value = record[key] ?? record[`market_${key}`] ?? record[`${key}_ticker`];
  return value == null || value === "" ? "—" : String(value);
}

function LiveOrderRow({ order }: { order: LiveOrderRead }) {
  const { formatPrice } = usePriceDisplay();
  return (
    <TableRow>
      <TableCell>
        <span className="font-mono text-xs text-accent">{order.ticker}</span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{order.side.toUpperCase()}</span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{order.quantity}</span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{formatPrice(order.limit_price)}</span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{formatMoney(order.max_cost_cents)}</span>
      </TableCell>
      <TableCell>
        <Badge variant={statusVariant(order.status)}>{order.status}</Badge>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs text-muted-foreground">{fmtDatetime(order.submitted_at)}</span>
      </TableCell>
    </TableRow>
  );
}

function LiveAccountBody({ data, onRefresh, busy }: { data: KalshiAccountRead; onRefresh: () => void; busy: boolean }) {
  const positions = rawPositions(data);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-1.5">
          <Badge variant={data.credentials_configured ? "positive" : "warning"}>
            {data.credentials_configured ? "Live Credentials" : "No Live Credentials"}
          </Badge>
          <Badge variant="outline">{data.environment}</Badge>
        </div>
        <Button variant="secondary" size="sm" onClick={onRefresh} disabled={busy || !data.credentials_configured}>
          Refresh Account
        </Button>
      </div>

      <div className="grid gap-2 sm:grid-cols-4">
        <Metric label="Cash Balance" value={formatMoney(data.snapshot?.balance_cents)} />
        <Metric label="Portfolio Value" value={formatMoney(data.snapshot?.portfolio_value_cents)} />
        <Metric label="Live Positions" value={`${data.snapshot?.open_positions_count ?? positions.length}`} />
        <Metric label="Open Orders" value={`${data.snapshot?.open_orders_count ?? 0}`} />
      </div>

      <div className="space-y-2">
        <p className="text-xs font-medium text-muted-foreground">Live positions</p>
        {positions.length === 0 ? (
          <p className="rounded border border-border px-3 py-3 text-sm text-muted-foreground">
            No live positions in the latest account snapshot.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Ticker</TableHead>
                <TableHead>Position</TableHead>
                <TableHead>Cost</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {positions.map((position, index) => (
                <TableRow key={index}>
                  <TableCell>
                    <span className="font-mono text-xs">{positionText(position, "ticker")}</span>
                  </TableCell>
                  <TableCell>
                    <span className="font-mono text-xs">{positionText(position, "position")}</span>
                  </TableCell>
                  <TableCell>
                    <span className="font-mono text-xs">{positionText(position, "cost")}</span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <div className="space-y-2">
        <p className="text-xs font-medium text-muted-foreground">Live orders</p>
        {data.live_orders.length === 0 ? (
          <p className="rounded border border-border px-3 py-3 text-sm text-muted-foreground">
            No live orders have been recorded.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Ticker</TableHead>
                <TableHead>Side</TableHead>
                <TableHead>Qty</TableHead>
                <TableHead>Limit</TableHead>
                <TableHead>Max Cost</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Submitted</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.live_orders.map((order) => (
                <LiveOrderRow key={order.id} order={order} />
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}

export function LiveAccountPanel() {
  const admin = useAdminToken();
  const [busy, setBusy] = useState(false);
  const account = useSWR(
    admin.hasToken ? [keys.kalshiAccount, admin.token] : null,
    ([, token]) => fetchKalshiAccount(token, false),
  );

  const handleRefresh = async () => {
    setBusy(true);
    try {
      const next = await fetchKalshiAccount(admin.token, true);
      account.mutate(next, false);
    } finally {
      setBusy(false);
    }
  };

  if (!admin.loaded) {
    return (
      <Card>
        <CardContent>
          <Skeleton className="h-24 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (!admin.hasToken) {
    return (
      <AdminTokenCard
        title="Live Kalshi Account"
        description="Enter the owner admin token to view live account data."
        onSubmit={admin.setToken}
      />
    );
  }

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Live Kalshi Account</CardTitle>
          <CardDescription>Production account data stays behind the owner token.</CardDescription>
        </div>
        <Button variant="ghost" size="sm" onClick={admin.clearToken}>
          Lock
        </Button>
      </CardHeader>
      <CardContent>
        {account.error ? (
          <div className="space-y-3">
            <p className="rounded border border-negative/20 bg-negative/10 px-3 py-2 text-sm text-negative">
              {account.error.message}
            </p>
            <Button variant="secondary" size="sm" onClick={admin.clearToken}>
              Re-enter Token
            </Button>
          </div>
        ) : !account.data ? (
          <Skeleton className="h-32 w-full" />
        ) : (
          <LiveAccountBody data={account.data} onRefresh={handleRefresh} busy={busy} />
        )}
      </CardContent>
    </Card>
  );
}
