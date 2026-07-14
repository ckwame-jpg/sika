"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { RefreshCw } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  getMarketSyncBadge,
  getSyncState,
  useHealthStatus,
} from "@/lib/health-status";

type SyncState = "queued" | "refreshing" | "worker_offline" | "stalled" | "failed" | "stale" | "synced";
import { RefreshAbortError, triggerRefreshAndRevalidate } from "@/lib/refresh";
import { SPORT_OPTIONS, cn } from "@/lib/utils";
import { useSportQueryParam } from "@/components/filters/sport-filter-select";
import { OrbitMark } from "./orbit-mark";

/* Orbit-dock nav (glass-instrument spec §Sidebar): every primary item
   is a tiny orbital in its own accent, carrying a live badge. */
const PRIMARY_NAV = [
  { href: "/trade", label: "trade", accent: "var(--color-cosmos-cyan-500)" },
  { href: "/events", label: "events", accent: "var(--gi-green-pale)" },
  { href: "/predictions", label: "predictions", accent: "var(--color-cosmos-violet-300)" },
  { href: "/positions", label: "portfolio", accent: "var(--gi-magenta)" },
  { href: "/stats", label: "stats", accent: "var(--gi-orange)" },
];

const OPS_NAV = [
  { href: "/runs", label: "runs" },
  { href: "/mappings", label: "mappings" },
  { href: "/settings", label: "settings" },
];

function isActivePath(pathname: string, href: string, exact: boolean) {
  if (exact) return pathname === href;
  return pathname === href || pathname.startsWith(`${href}/`);
}

function NavItem({
  href,
  label,
  accent,
  badge,
  exact = false,
  onNavigate,
}: {
  href: string;
  label: string;
  accent: string;
  badge?: React.ReactNode;
  exact?: boolean;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const active = isActivePath(pathname, href, exact);

  return (
    <Link
      href={href}
      onClick={onNavigate}
      className={cn("nav-item", active && "active")}
      style={{ "--na": accent } as React.CSSProperties}
    >
      <span className="nav-orbital" aria-hidden />
      <span>{label}</span>
      {badge}
    </Link>
  );
}

function sportTint(value: string): string {
  return `var(--sport-${value.toLowerCase()})`;
}

function SportFilter({ onNavigate }: { onNavigate?: () => void }) {
  const { sport, setSport } = useSportQueryParam();
  const currentSport = sport ?? "";

  function handleSelect(nextSport: string) {
    setSport(nextSport);
    onNavigate?.();
  }

  return (
    <div className="nav-section">
      <div className="nav-label">Sport</div>
      <button
        type="button"
        onClick={() => handleSelect("")}
        className={cn("nav-item sport-row", currentSport === "" && "active")}
      >
        <span className="dot" />
        <span>All Sports</span>
      </button>
      {SPORT_OPTIONS.map((option) => {
        const isActive = currentSport === option.value;
        return (
          <button
            key={option.value}
            type="button"
            onClick={() => handleSelect(option.value)}
            className={cn("nav-item sport-row", isActive && "active")}
          >
            <span
              className="dot"
              style={isActive ? undefined : { color: sportTint(option.value) }}
            />
            <span>{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}

function OpsNav({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const { data: health } = useHealthStatus();
  const syncState = getSyncState(health);
  const runBusy = syncState === "queued" || syncState === "refreshing";

  return (
    <div className="nav-section">
      <div className="nav-label">Operator</div>
      {OPS_NAV.map((item) => {
        const active = isActivePath(pathname, item.href, false);
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            className={cn("nav-item", active && "active")}
          >
            <span>{item.label}</span>
            {item.href === "/runs" && (
              <span
                className={cn("nav-status-dot", runBusy && "busy")}
                title={runBusy ? "run in progress" : "no run in progress"}
                aria-hidden
              />
            )}
          </Link>
        );
      })}
    </div>
  );
}

function syncLabel(state: SyncState | null): string {
  switch (state) {
    case "queued":
      return "Refresh queued";
    case "refreshing":
      return "Refreshing…";
    case "worker_offline":
      return "Worker offline";
    case "stalled":
      return "Refresh stalled";
    case "failed":
      return "Refresh failed";
    case "stale":
      return "Stale data";
    case "synced":
      return "Orbits aligned";
    default:
      return "Awaiting sync";
  }
}

function SyncFoot() {
  const [refreshing, setRefreshing] = useState(false);
  // Bug #35 — refresh poll runs for up to 40 minutes. Without an
  // AbortController, unmount or navigation left the loop running.
  // Track the in-flight controller and abort it on unmount.
  const refreshControllerRef = useRef<AbortController | null>(null);
  const { data: health } = useHealthStatus();
  const syncState = getSyncState(health);
  const marketBadge = getMarketSyncBadge(health);

  const label = syncLabel(syncState);
  const sub = marketBadge?.text ?? "markets";
  const title = marketBadge?.title ?? "";

  useEffect(() => {
    return () => {
      refreshControllerRef.current?.abort();
    };
  }, []);

  async function handleRefresh() {
    refreshControllerRef.current?.abort();  // cancel any previous in-flight poll
    const controller = new AbortController();
    refreshControllerRef.current = controller;
    setRefreshing(true);
    try {
      await triggerRefreshAndRevalidate({ signal: controller.signal });
    } catch (err) {
      // Aborts are expected on unmount / re-click; swallow quietly.
      if (!(err instanceof RefreshAbortError)) {
        /* other errors already surface via toast / health badge */
      }
    } finally {
      if (refreshControllerRef.current === controller) {
        refreshControllerRef.current = null;
      }
      setRefreshing(false);
    }
  }

  const isBusy = refreshing || syncState === "queued" || syncState === "refreshing" || syncState === "worker_offline";

  return (
    <div className="sync-pill" title={title}>
      <div className="sync-orb" aria-hidden>
        <div className="sync-core" />
      </div>
      <div className="sync-meta">
        <span className="sync-label">{label}</span>
        <span className="sync-sub">{sub}</span>
      </div>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className={cn("sync-refresh", isBusy && "spin")}
            onClick={handleRefresh}
            disabled={isBusy}
            aria-label="Queue fast refresh"
          >
            <RefreshCw size={12} />
          </button>
        </TooltipTrigger>
        <TooltipContent side="right">
          {isBusy
            ? "A current-slate refresh is already queued or running."
            : "Queue a fast current-slate refresh."}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}

function SidebarBody({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <>
      <div className="sidebar-cosmos-brand">
        <OrbitMark />
        <span className="brand-name">sika</span>
      </div>

      <div className="sidebar-cosmos-body">
        <div className="nav-section">
          {PRIMARY_NAV.map((item) => (
            <NavItem key={item.href} {...item} onNavigate={onNavigate} />
          ))}
        </div>

        <SportFilter onNavigate={onNavigate} />

        <OpsNav onNavigate={onNavigate} />
      </div>

      <div className="sidebar-cosmos-foot">
        <SyncFoot />
      </div>
    </>
  );
}

export function Sidebar() {
  return (
    <aside className="sidebar">
      <SidebarBody />
    </aside>
  );
}
