"use client";

import { useState, type ReactNode } from "react";
import useSWR, { mutate } from "swr";
import { ChevronRight, RefreshCw } from "lucide-react";
import { fetchPositions, keys } from "@/lib/api";
import type {
  KalshiAccountFillRead,
  KalshiAccountMarketPositionRead,
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
import { cn, fmtDatetime, pnlClass, sideClass } from "@/lib/utils";

function fmtDollars(value: number | null | undefined): string {
  if (value == null) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function fmtSignedDollars(value: number | null | undefined): string {
  if (value == null) return "—";
  const sign = value >= 0 ? "+" : "";
  return `${sign}${fmtDollars(value)}`;
}

function fmtContracts(value: number | null | undefined): string {
  if (value == null) return "—";
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function fmtUnixTimestamp(ts: number | null | undefined): string {
  if (!ts) return "—";
  return fmtDatetime(new Date(ts * 1000).toISOString());
}

function accountStatusClass(status: string): string {
  if (status === "connected") return "settled";
  if (status === "error") return "lost";
  return "pending";
}

function FillPrice({ fill }: { fill: KalshiAccountFillRead }) {
  // Both yes_price_dollars and no_price_dollars are populated on every fill
  // (they are complements), so the old `??` fallback always rendered the YES
  // price — even for NO fills. Select the price actually paid, by side.
  const yes = fill.yes_price_dollars ?? null;
  const no = fill.no_price_dollars ?? (yes != null ? 1 - yes : null);
  const price = fill.side?.toLowerCase() === "no" ? no : yes ?? no;
  return <span className="font-mono text-xs">{fmtDollars(price)}</span>;
}

function BetCell({
  ticker,
  betLabel,
  betSubtitle,
  marketTitle,
  marketSubtitle,
}: {
  ticker: string;
  betLabel: string | null;
  betSubtitle: string | null;
  marketTitle: string | null;
  marketSubtitle: string | null;
}) {
  const label = betLabel || marketTitle || ticker;
  const subtitle =
    betSubtitle || marketSubtitle || (marketTitle && marketTitle !== label ? marketTitle : null);
  const showTicker = label !== ticker;

  return (
    <div className="min-w-0">
      <p className="truncate text-xs font-medium text-foreground">{label}</p>
      {subtitle && (
        <p className="mt-1 truncate text-xs text-muted-foreground">{subtitle}</p>
      )}
      {showTicker && (
        <p className="mt-1 truncate font-mono text-[11px] text-accent">{ticker}</p>
      )}
    </div>
  );
}

function PositionRow({ position }: { position: KalshiAccountMarketPositionRead }) {
  return (
    <TableRow>
      <TableCell>
        <BetCell
          ticker={position.ticker}
          betLabel={position.bet_label}
          betSubtitle={position.bet_subtitle}
          marketTitle={position.market_title}
          marketSubtitle={position.market_subtitle}
        />
      </TableCell>
      <TableCell>
        <span
          className={cn(
            "font-mono text-xs font-medium",
            position.position >= 0 ? sideClass("yes") : sideClass("no"),
          )}
        >
          {fmtContracts(position.position)}
        </span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{fmtDollars(position.market_exposure_dollars)}</span>
      </TableCell>
      <TableCell>
        <span className={cn("font-mono text-xs font-medium", pnlClass(position.realized_pnl_dollars))}>
          {fmtSignedDollars(position.realized_pnl_dollars)}
        </span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs text-muted-foreground">
          {position.resting_orders_count}
        </span>
      </TableCell>
    </TableRow>
  );
}

function FillRow({ fill }: { fill: KalshiAccountFillRead }) {
  return (
    <TableRow>
      <TableCell>
        <BetCell
          ticker={fill.ticker}
          betLabel={fill.bet_label}
          betSubtitle={fill.bet_subtitle}
          marketTitle={fill.market_title}
          marketSubtitle={fill.market_subtitle}
        />
      </TableCell>
      <TableCell>
        <span
          className={cn(
            "font-mono text-xs font-medium",
            fill.side ? sideClass(fill.side) : "text-muted-foreground",
          )}
        >
          {(fill.side ?? "—").toUpperCase()}
        </span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{fill.action ?? "—"}</span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs">{fmtContracts(fill.count)}</span>
      </TableCell>
      <TableCell>
        <FillPrice fill={fill} />
      </TableCell>
      <TableCell>
        <span className="font-mono text-xs text-muted-foreground">
          {fmtDatetime(fill.created_time)}
        </span>
      </TableCell>
    </TableRow>
  );
}

interface CollapsibleAccountSectionProps {
  title: string;
  count: number;
  expanded: boolean;
  onToggle: () => void;
  panelId: string;
  children: ReactNode;
}

function CollapsibleAccountSection({
  title,
  count,
  expanded,
  onToggle,
  panelId,
  children,
}: CollapsibleAccountSectionProps) {
  return (
    <section className="min-w-0">
      <button
        type="button"
        className="mb-2 flex w-full items-center justify-between gap-2 rounded px-1 py-1 text-left transition-colors duration-[120ms] hover:bg-surface-hover focus-visible:ring-focus"
        aria-label={`${title} ${count}`}
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={onToggle}
      >
        <span className="flex min-w-0 items-center gap-2">
          <ChevronRight
            size={14}
            aria-hidden
            className={cn("shrink-0 text-muted-foreground transition-transform", expanded && "rotate-90")}
          />
          <span className="text-sm font-medium text-foreground">{title}</span>
        </span>
        <span className="font-mono text-xs text-muted-foreground">{count}</span>
      </button>
      <div id={panelId} hidden={!expanded}>
        {expanded ? children : null}
      </div>
    </section>
  );
}

interface MetricProps {
  label: string;
  value: string;
  tone?: "default" | "positive" | "negative";
  testId?: string;
}

function Metric({ label, value, tone = "default", testId }: MetricProps) {
  return (
    <div className="rounded border border-border px-3 py-2.5">
      <p className="text-[11px] uppercase text-muted-foreground">{label}</p>
      <p
        data-testid={testId}
        className={cn(
          "mt-1 font-mono text-sm font-semibold",
          tone === "positive" && "text-positive",
          tone === "negative" && "text-negative",
        )}
      >
        {value}
      </p>
    </div>
  );
}

export function KalshiAccountPanel() {
  const [openPicksExpanded, setOpenPicksExpanded] = useState(true);
  const [recentFillsExpanded, setRecentFillsExpanded] = useState(false);
  // Bug #6, codex round-12 P2: track in-flight force-refresh state
  // locally. ``mutate(key, promise, { revalidate: false })`` does NOT
  // set SWR's ``isValidating``, so without this local flag the
  // Refresh button stays clickable during the force fetch — a rapid
  // double-click would spawn multiple ``/positions?force=true``
  // requests, each expiring the backend cache and re-fetching from
  // Kalshi (undermining the rate-limit protection).
  const [isForcing, setIsForcing] = useState(false);
  const { data, isLoading, error, isValidating } = useSWR<PositionsRead>(
    keys.positions,
    fetchPositions,
    { refreshInterval: 15_000 },
  );
  const account = data?.kalshi_account;
  const positions = account?.market_positions ?? [];
  const fills = account?.recent_fills ?? [];
  const realizedPnl = positions.reduce(
    (total, position) => total + (position.realized_pnl_dollars ?? 0),
    0,
  );
  const isRefreshing = isValidating || isForcing;

  async function refresh() {
    // Bug #6, codex round-5 P2: backend caches the Kalshi account
    // snapshot for ~30 s to throttle the 15 s polling cadence.
    // User-initiated refreshes pass ``force=true`` so they bypass the
    // cache instead of seeing stale data until the next auto-poll.
    if (isForcing) return;
    setIsForcing(true);
    try {
      await mutate(keys.positions, fetchPositions({ force: true }), {
        revalidate: false,
      });
    } finally {
      setIsForcing(false);
    }
  }

  // Only hard-fail when there's no cached data. SWR keeps the last good
  // snapshot on a failed poll (stale-if-error), and /positions is the slowest
  // endpoint, so a transient timeout shouldn't vanish the balance + positions.
  if (error && !data) {
    return (
      <div className="flex h-24 items-center justify-center text-xs text-negative">
        Failed to load Kalshi account.
      </div>
    );
  }

  if (isLoading || !account) {
    return (
      <div className="space-y-4 p-4">
        <div className="grid gap-3 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, index) => (
            <Skeleton key={index} className="h-16 w-full" />
          ))}
        </div>
        <div className="cosmos-table-wrap">
          <Table>
            <TableBody>
              {Array.from({ length: 4 }).map((_, index) => (
                <SkeletonRow key={index} cols={5} />
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
    );
  }

  if (account.status !== "connected") {
    return (
      <div className="flex min-h-40 flex-col items-center justify-center gap-3 p-4 text-center">
        <span className={cn("outcome-pill", accountStatusClass(account.status))}>
          {account.status === "not_configured" ? "Not configured" : "Sync error"}
        </span>
        <p className="max-w-xl text-sm text-muted-foreground">
          {account.error_message ?? "Kalshi account data is unavailable."}
        </p>
        <Button variant="ghost" size="sm" onClick={refresh} disabled={isRefreshing} className="gap-1.5">
          <RefreshCw size={13} className={cn(isRefreshing && "animate-spin")} />
          Refresh
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4 p-4" data-testid="kalshi-account-panel">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className={cn("outcome-pill", accountStatusClass(account.status))}>Connected</span>
          <span className="text-xs text-muted-foreground">
            Updated {fmtUnixTimestamp(account.balance?.updated_ts)}
          </span>
        </div>
        <Button variant="ghost" size="sm" onClick={refresh} disabled={isRefreshing} className="gap-1.5">
          <RefreshCw size={13} className={cn(isRefreshing && "animate-spin")} />
          Refresh
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Cash" value={fmtDollars(account.balance?.cash_balance_dollars)} />
        <Metric label="Portfolio" value={fmtDollars(account.balance?.portfolio_value_dollars)} />
        <Metric label="Open Picks" value={String(positions.length)} testId="kalshi-open-picks" />
        <Metric
          label="Realized PnL"
          value={fmtSignedDollars(realizedPnl)}
          tone={realizedPnl >= 0 ? "positive" : "negative"}
        />
      </div>

      <div className="grid gap-4 2xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        <CollapsibleAccountSection
          title="Open Picks"
          count={positions.length}
          expanded={openPicksExpanded}
          onToggle={() => setOpenPicksExpanded((current) => !current)}
          panelId="kalshi-open-picks-table"
        >
          <div className="cosmos-table-wrap">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Bet</TableHead>
                  <TableHead className="w-20">Position</TableHead>
                  <TableHead className="w-24">Exposure</TableHead>
                  <TableHead className="w-24">Realized</TableHead>
                  <TableHead className="w-20">Resting</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {positions.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={5} className="cosmos-table-empty">
                      No open Kalshi picks
                    </TableCell>
                  </TableRow>
                ) : (
                  positions.map((position) => (
                    <PositionRow key={position.ticker} position={position} />
                  ))
                )}
              </TableBody>
            </Table>
          </div>
        </CollapsibleAccountSection>

        <CollapsibleAccountSection
          title="Recent Fills"
          count={fills.length}
          expanded={recentFillsExpanded}
          onToggle={() => setRecentFillsExpanded((current) => !current)}
          panelId="kalshi-recent-fills-table"
        >
          <div className="cosmos-table-wrap">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Bet</TableHead>
                  <TableHead className="w-16">Side</TableHead>
                  <TableHead className="w-20">Action</TableHead>
                  <TableHead className="w-16">Qty</TableHead>
                  <TableHead className="w-20">Price</TableHead>
                  <TableHead className="w-32">Filled</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {fills.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={6} className="cosmos-table-empty">
                      No recent fills
                    </TableCell>
                  </TableRow>
                ) : (
                  fills.map((fill) => (
                    <FillRow key={fill.fill_id ?? `${fill.ticker}-${fill.created_time}`} fill={fill} />
                  ))
                )}
              </TableBody>
            </Table>
          </div>
        </CollapsibleAccountSection>
      </div>
    </div>
  );
}
