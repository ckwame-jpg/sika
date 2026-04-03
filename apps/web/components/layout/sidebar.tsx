"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Calendar,
  Star,
  CandlestickChart,
  BarChart3,
  DatabaseZap,
  FileText,
  ClipboardList,
  RefreshCw,
  Activity,
  ChevronRight,
  Target,
  Settings2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { triggerRefresh } from "@/lib/api";
import { getSyncState, useHealthStatus } from "@/lib/health-status";
import { SPORT_OPTIONS, cn, fmtRelative } from "@/lib/utils";
import { useState } from "react";
import { mutate } from "swr";
import { useSportQueryParam } from "@/components/filters/sport-filter-select";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard, exact: true },
  { href: "/watchlist", label: "Watchlist", icon: Star },
  { href: "/markets", label: "Markets", icon: CandlestickChart },
  { href: "/stats", label: "Stats", icon: BarChart3 },
  { href: "/runs", label: "Runs", icon: DatabaseZap },
  { href: "/predictions", label: "Predictions", icon: Target },
  { href: "/events", label: "Events", icon: Calendar },
  { href: "/settings", label: "Settings", icon: Settings2 },
];

const POSITIONS_NAV = [
  { href: "/positions", label: "Paper", icon: FileText, exact: true },
  { href: "/positions/demo", label: "Demo Orders", icon: ClipboardList, exact: true },
];

function isActivePath(pathname: string, href: string, exact: boolean) {
  if (exact) return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}

function NavItem({
  href,
  label,
  icon: Icon,
  exact = false,
}: {
  href: string;
  label: string;
  icon: React.ElementType;
  exact?: boolean;
}) {
  const pathname = usePathname();
  const active = isActivePath(pathname, href, exact);

  return (
    <Link
      href={href}
      className={cn(
        "flex items-center gap-2.5 rounded px-2.5 py-1.5 text-sm",
        "transition-colors duration-[120ms]",
        active
          ? "bg-accent/10 font-medium text-accent"
          : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
      )}
    >
      <Icon size={14} className="shrink-0" />
      <span>{label}</span>
      {active && <ChevronRight size={12} className="ml-auto opacity-60" />}
    </Link>
  );
}

function SportFilter() {
  const { sport, setSport } = useSportQueryParam();
  const currentSport = sport ?? "";

  return (
    <div className="space-y-0.5">
      <button
        onClick={() => setSport("")}
        className={cn(
          "flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-sm",
          "transition-colors duration-[120ms]",
          currentSport === ""
            ? "font-medium text-foreground"
            : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
        )}
      >
        <Activity size={13} />
        All Sports
      </button>
      {SPORT_OPTIONS.map((option) => (
        <button
          key={option.value}
          onClick={() => setSport(option.value)}
          className={cn(
            "flex w-full items-center gap-2 rounded px-2.5 py-1.5 text-sm",
            "transition-colors duration-[120ms]",
            currentSport === option.value
              ? cn("font-medium", option.colorClass)
              : "text-muted-foreground hover:bg-surface-hover hover:text-foreground",
          )}
        >
          <span
            className={cn(
              "inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-current",
              currentSport === option.value ? option.colorClass : "opacity-40",
            )}
          />
          {option.label}
        </button>
      ))}
    </div>
  );
}

function SyncStatusBadge() {
  const { data: health } = useHealthStatus();
  const syncState = getSyncState(health);
  if (!health || !syncState) return null;

  const label = syncState === "refreshing"
    ? "Refreshing"
    : syncState === "failed"
      ? "Failed"
      : syncState === "stale"
        ? "Stale"
        : "Synced";
  const dotClass = syncState === "synced"
    ? "bg-positive"
    : syncState === "failed"
      ? "bg-negative"
      : "bg-warning";
  const detail = syncState === "refreshing"
    ? (health.last_successful_refresh_at ? `last success ${fmtRelative(health.last_successful_refresh_at)}` : "background refresh in progress")
    : health.last_successful_refresh_at
      ? fmtRelative(health.last_successful_refresh_at)
      : "awaiting first refresh";

  return (
    <div className="flex items-center gap-1.5 text-xs">
      <span className={cn("inline-block h-1.5 w-1.5 rounded-full", dotClass)} />
      <span className="truncate text-muted-foreground">
        {label} {detail}
      </span>
    </div>
  );
}

export function Sidebar() {
  const [refreshing, setRefreshing] = useState(false);
  const { data: health } = useHealthStatus();
  const syncState = getSyncState(health);

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await triggerRefresh();
      await Promise.all([
        mutate((key) => typeof key === "string" && key.startsWith("/runs")),
        mutate((key) => typeof key === "string" && key.startsWith("/events")),
        mutate((key) => typeof key === "string" && key.startsWith("/watchlist")),
        mutate((key) => typeof key === "string" && key.startsWith("/markets")),
        mutate((key) => typeof key === "string" && key.startsWith("/positions")),
        mutate((key) => typeof key === "string" && key.startsWith("/predictions")),
        mutate((key) => typeof key === "string" && key.startsWith("/parlays")),
        mutate("/health"),
      ]);
    } catch {
      /* ignore */
    } finally {
      setTimeout(() => setRefreshing(false), 1200);
    }
  }

  return (
    <aside className="flex h-full w-56 flex-col border-r border-border bg-surface">
      <div className="flex items-center gap-2.5 border-b border-border px-4 py-4">
        <div className="flex h-6 w-6 items-center justify-center rounded border border-accent/25 bg-accent/15">
          <Activity size={12} className="text-accent" />
        </div>
        <div className="flex flex-col">
          <span className="text-sm font-semibold tracking-tight text-foreground">
            sika
          </span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto space-y-4 px-2 py-3">
        <nav className="space-y-0.5">
          {NAV.map((item) => (
            <NavItem key={item.href} {...item} />
          ))}
        </nav>

        <div>
          <p className="px-2.5 pb-1 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            Positions
          </p>
          <nav className="space-y-0.5">
            {POSITIONS_NAV.map((item) => (
              <NavItem key={item.href} {...item} />
            ))}
          </nav>
        </div>

        <div>
          <p className="px-2.5 pb-1 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            Sport
          </p>
          <SportFilter />
        </div>
      </div>

      <div className="space-y-2 border-t border-border px-3 py-3">
        <SyncStatusBadge />
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              className="w-full justify-start gap-2 text-muted-foreground"
              onClick={handleRefresh}
              disabled={refreshing || syncState === "refreshing"}
            >
              <RefreshCw
                size={13}
                className={cn((refreshing || syncState === "refreshing") && "animate-spin")}
              />
              {syncState === "refreshing" ? "Refreshing" : "Run refresh"}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="right">
            {syncState === "refreshing"
              ? "A refresh is already running in the background."
              : "Trigger a data refresh cycle."}
          </TooltipContent>
        </Tooltip>
      </div>
    </aside>
  );
}
