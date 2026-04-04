"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { ArrowRight } from "lucide-react";
import { Header } from "@/components/layout/header";
import { EventsFeed } from "@/components/events/events-feed";
import { PaperPositionsTable } from "@/components/positions/paper-positions-table";
import { Card, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { MarketDetailSheet } from "@/components/markets/market-detail-sheet";
import { SportFilterSelect, useSportQueryParam } from "@/components/filters/sport-filter-select";
import { fetchPositions, fetchWatchlist, keys } from "@/lib/api";
import type { PositionsRead, RecommendationRead } from "@/lib/types";
import { cn, edgeClass, fmtContractPnl, fmtEdge, sideClass, sportLabel } from "@/lib/utils";
import { usePriceDisplay } from "@/lib/price-display";

function StatCard({
  label,
  value,
  sub,
  valueClass,
}: {
  label: string;
  value: string;
  sub?: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn("mt-0.5 font-mono text-xl font-medium text-foreground", valueClass)}>
        {value}
      </p>
      {sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}
    </div>
  );
}

function TopEdgeItems({ sport }: { sport?: string }) {
  const { formatPrice } = usePriceDisplay();
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const { data } = useSWR<RecommendationRead[]>(
    keys.watchlist(sport, 5),
    () => fetchWatchlist(sport, 5),
    { refreshInterval: 30_000 },
  );

  const items = data ?? [];
  const emptyMessage = sport
    ? `No ${sportLabel(sport)} watchlist items`
    : "No watchlist items";

  return (
    <>
      <div className="space-y-1">
        {items.length === 0 && (
          <p className="py-2 text-xs text-muted-foreground">{emptyMessage}</p>
        )}
        {items.map((rec) => (
          <button
            key={rec.id}
            className="flex w-full items-center gap-3 rounded px-2 py-2 text-left transition-colors duration-[120ms] hover:bg-surface-hover"
            onClick={() => setSelectedTicker(rec.ticker)}
          >
            <span className={cn("shrink-0 font-mono text-xs font-medium", sideClass(rec.side))}>
              {rec.side.toUpperCase()}
            </span>
            <span className="flex-1 truncate text-xs text-foreground">{rec.market_title}</span>
            <span className="shrink-0 font-mono text-xs text-muted-foreground">
              {formatPrice(rec.suggested_price)}
            </span>
            <span className={cn("shrink-0 font-mono text-xs font-medium", edgeClass(rec.edge))}>
              {fmtEdge(rec.edge)}
            </span>
          </button>
        ))}
      </div>
      <MarketDetailSheet
        ticker={selectedTicker}
        onClose={() => setSelectedTicker(null)}
      />
    </>
  );
}

function DashboardStats() {
  const { data } = useSWR<PositionsRead>(keys.positions, fetchPositions, {
    refreshInterval: 30_000,
  });

  const openPositions =
    data?.paper_positions.filter((position) => position.status === "open").length ?? "—";
  const closedPnl =
    data?.paper_positions
      .filter((position) => position.status !== "open" && position.pnl != null)
      .reduce((acc, position) => acc + (position.pnl ?? 0), 0) ?? null;
  const demoOrders = data?.demo_orders.length ?? "—";
  const pendingOrders =
    data?.demo_orders.filter((order) => order.status === "pending").length ?? "—";

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <StatCard
        label="Open Positions"
        value={String(openPositions)}
        sub="paper trading"
      />
      <StatCard
        label="Realized PnL"
        value={fmtContractPnl(closedPnl)}
        sub="closed positions"
        valueClass={
          closedPnl != null
            ? closedPnl >= 0
              ? "text-positive"
              : "text-negative"
            : undefined
        }
      />
      <StatCard
        label="Demo Orders"
        value={String(demoOrders)}
        sub="total submitted"
      />
      <StatCard
        label="Pending Orders"
        value={String(pendingOrders)}
        sub="awaiting fill"
        valueClass={
          typeof pendingOrders === "number" && pendingOrders > 0
            ? "text-warning"
            : undefined
        }
      />
    </div>
  );
}

function DashboardContent() {
  const { sport } = useSportQueryParam();
  const eventsHref = sport ? `/events?sport=${sport}` : "/events";
  const watchlistHref = sport ? `/watchlist?sport=${sport}` : "/watchlist";

  return (
    <div className="flex min-h-full flex-col gap-4 p-3 sm:p-4">
      <DashboardStats />

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <Card>
          <CardHeader className="flex-wrap gap-3">
            <div>
              <CardTitle>Live Events</CardTitle>
              <CardDescription>
                {sport ? `${sportLabel(sport)} · live & upcoming · 30s refresh` : "All sports · live & upcoming · 30s refresh"}
              </CardDescription>
            </div>
            <div className="grid w-full gap-2 sm:flex sm:w-auto sm:items-center">
              <SportFilterSelect triggerClassName="h-7 w-full text-xs sm:w-[140px]" />
              <Button variant="ghost" size="sm" asChild>
                <Link href={eventsHref} className="flex items-center gap-1">
                  All events
                  <ArrowRight size={12} />
                </Link>
              </Button>
            </div>
          </CardHeader>
          <div className="px-4 pb-4">
            <EventsFeed sport={sport} compact mode="dashboard" />
          </div>
        </Card>

        <div className="flex flex-col gap-4">
          <Card className="flex flex-col" style={{ maxHeight: "260px" }}>
            <CardHeader>
              <div>
                <CardTitle>Open Positions</CardTitle>
                <CardDescription>Paper trading</CardDescription>
              </div>
              <Button variant="ghost" size="sm" asChild>
                <Link href="/positions" className="flex items-center gap-1">
                  Manage
                  <ArrowRight size={12} />
                </Link>
              </Button>
            </CardHeader>
            <div className="flex-1 overflow-hidden px-4 pb-3">
              <PaperPositionsTable maxHeight="200px" />
            </div>
          </Card>

          <Card>
            <CardHeader>
              <div>
                <CardTitle>Top Edge</CardTitle>
                <CardDescription>
                  {sport ? `Highest-edge ${sportLabel(sport)} recommendations` : "Highest-edge recommendations"}
                </CardDescription>
              </div>
              <Button variant="ghost" size="sm" asChild>
                <Link href={watchlistHref} className="flex items-center gap-1">
                  Watchlist
                  <ArrowRight size={12} />
                </Link>
              </Button>
            </CardHeader>
            <div className="px-4 pb-3">
              <TopEdgeItems sport={sport} />
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

export default function DashboardPage() {
  return (
    <>
      <Header
        title="Dashboard"
        description="Live events & open positions"
      />
      <main className="flex-1 overflow-y-auto">
        <Suspense>
          <DashboardContent />
        </Suspense>
      </main>
    </>
  );
}
