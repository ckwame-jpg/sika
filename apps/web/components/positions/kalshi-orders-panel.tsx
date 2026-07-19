"use client";

import { useState } from "react";
import useSWR, { mutate } from "swr";
import { cancelKalshiOrder, fetchKalshiOrders, fetchMyKalshiCredentials, keys } from "@/lib/api";
import type { KalshiOrderRead } from "@/lib/types";
import { usePriceDisplay } from "@/lib/price-display";
import { cn } from "@/lib/utils";
import { RefreshCw } from "lucide-react";

/** Statuses the operator can still act on (cancel). */
const CANCELLABLE = new Set(["resting", "pending_submission", "submitting"]);
const TERMINAL_BAD = new Set(["submission_failed", "mint_failed"]);

/**
 * Real Kalshi orders — the live-money sibling of the demo-orders view.
 * Shows every order the user placed through sika (singles now, combos
 * in phase E), its environment, status, fill progress, and a cancel
 * action while it rests. Failures surface ``error_detail`` inline so a
 * dead-lettered submit is never silent.
 */
export function KalshiOrdersPanel() {
  const { data: creds } = useSWR(keys.myKalshiCredentials, fetchMyKalshiCredentials);
  const { data: orders, isLoading } = useSWR(
    creds?.configured ? keys.kalshiOrders : null,
    () => fetchKalshiOrders(),
    { refreshInterval: 15_000 },
  );
  const [syncing, setSyncing] = useState(false);
  const [cancellingId, setCancellingId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  if (!creds?.configured) return null;

  async function refresh() {
    if (syncing) return;
    setSyncing(true);
    setActionError(null);
    try {
      // sync=true reconciles against Kalshi inline before responding.
      await mutate(keys.kalshiOrders, fetchKalshiOrders({ sync: true }), {
        revalidate: false,
      });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Sync failed");
    } finally {
      setSyncing(false);
    }
  }

  async function handleCancel(id: number) {
    setCancellingId(id);
    setActionError(null);
    try {
      await cancelKalshiOrder(id);
      await mutate(keys.kalshiOrders);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Cancel failed");
    } finally {
      setCancellingId(null);
    }
  }

  return (
    <section className="gi-panel" data-testid="kalshi-orders-panel">
      <div className="gi-panel-head">
        <span
          className="gi-glow-dot"
          style={{ "--gd": "var(--gi-amber)" } as React.CSSProperties}
          aria-hidden
        />
        <h2 className="gi-panel-title">kalshi orders</h2>
        <span className="gi-panel-sub">placed through sika · limit orders may rest</span>
        <button
          type="button"
          className="gi-btn-ghost ml-auto"
          onClick={() => void refresh()}
          disabled={syncing}
          data-testid="kalshi-orders-sync"
        >
          <RefreshCw size={12} className={cn(syncing && "animate-spin")} />
          {syncing ? "syncing" : "sync"}
        </button>
      </div>

      {actionError && (
        <p className="px-[18px] pt-3 text-xs text-negative" role="alert">
          {actionError}
        </p>
      )}

      {isLoading && !orders ? (
        <p className="px-[18px] py-4 text-sm text-muted-foreground">Loading orders…</p>
      ) : !orders || orders.length === 0 ? (
        <p className="px-[18px] py-4 text-sm text-muted-foreground" data-testid="kalshi-orders-empty">
          No real orders yet — place one from the trade ticket.
        </p>
      ) : (
        <div>
          {orders.map((order) => (
            <OrderRow
              key={order.id}
              order={order}
              cancelling={cancellingId === order.id}
              onCancel={() => void handleCancel(order.id)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function OrderRow({
  order,
  cancelling,
  onCancel,
}: {
  order: KalshiOrderRead;
  cancelling: boolean;
  onCancel: () => void;
}) {
  const { formatPrice } = usePriceDisplay();
  const [legsOpen, setLegsOpen] = useState(false);
  const filled = order.fills.reduce((sum, fill) => sum + (fill.count ?? 0), 0);
  const isCombo = order.kind === "combo";
  const label = isCombo
    ? `combo · ${order.legs.length} legs`
    : order.ticker ?? order.client_order_id;

  return (
    <div
      className="border-t border-white/5 px-[18px] py-3 first:border-t-0"
      data-testid="kalshi-order-row"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
      <span className="min-w-0 flex-1">
        {isCombo ? (
          <button
            type="button"
            className="block truncate text-[13px] font-medium text-foreground hover:text-accent focus-visible:ring-focus"
            onClick={() => setLegsOpen((current) => !current)}
            aria-expanded={legsOpen}
            data-testid="kalshi-order-legs-toggle"
          >
            {label} {legsOpen ? "▾" : "▸"}
          </button>
        ) : (
          <span className="block truncate text-[13px] font-medium text-foreground">{label}</span>
        )}
        <span className="block truncate font-mono text-[10.5px] text-muted-foreground">
          {order.side.toUpperCase()} · {order.quantity} @ {formatPrice(order.limit_price)}
          {filled > 0 ? ` · filled ${filled}/${order.quantity}` : ""}
        </span>
        {TERMINAL_BAD.has(order.status) && order.error_detail && (
          <span className="mt-0.5 block truncate text-[10.5px] text-negative">
            {order.error_detail}
          </span>
        )}
      </span>
      <span
        className={cn(
          "rounded-full border px-2 py-0.5 font-mono text-2xs uppercase",
          order.environment === "live"
            ? "border-warning/40 text-warning"
            : "border-border/60 text-muted-foreground",
        )}
      >
        {order.environment}
      </span>
      <span
        className={cn(
          "rounded-full border px-2 py-0.5 font-mono text-2xs uppercase",
          order.status === "resting" && "border-accent/40 text-accent",
          order.status === "executed" && "border-positive/40 text-positive",
          (order.status === "cancelled" || order.status === "cancelling") &&
            "border-border/60 text-muted-foreground",
          TERMINAL_BAD.has(order.status) && "border-negative/40 text-negative",
          (order.status === "submitting" || order.status === "pending_submission") &&
            "border-border/60 text-muted-foreground",
        )}
        data-testid="kalshi-order-status"
      >
        {order.status.replace(/_/g, " ")}
      </span>
      {CANCELLABLE.has(order.status) && order.kalshi_order_id && (
        <button
          type="button"
          className="gi-btn-ghost"
          onClick={onCancel}
          disabled={cancelling}
          data-testid="kalshi-order-cancel"
        >
          {cancelling ? "cancelling…" : "cancel"}
        </button>
      )}
      </div>

      {isCombo && legsOpen && (
        <ul className="mt-2 space-y-1 pl-3" data-testid="kalshi-order-legs">
          {order.legs.map((leg) => (
            <li key={leg.id} className="truncate text-[11.5px] text-muted-foreground">
              <span className="font-mono text-[10px] uppercase">{leg.side}</span>{" "}
              <span className="text-foreground/90">
                {leg.market_title ??
                  (leg.subject_name && leg.stat_key && leg.threshold != null
                    ? `${leg.subject_name} ${leg.threshold}+ ${leg.stat_key.replace(/_/g, " ")}`
                    : leg.market_ticker)}
              </span>
              {leg.entry_price != null && (
                <span className="font-mono text-[10px]"> · {(leg.entry_price * 100).toFixed(0)}¢</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
