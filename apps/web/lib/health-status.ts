"use client";

import useSWR from "swr";
import { fetchHealth, keys } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";
import { fmtRelative } from "@/lib/utils";

type SyncState = "refreshing" | "stalled" | "failed" | "stale" | "synced";
type SyncBadgeVariant = "positive" | "warning" | "negative";

interface SyncBadge {
  text: string;
  title: string;
  variant: SyncBadgeVariant;
}

export function useHealthStatus() {
  return useSWR<HealthResponse>(keys.health, fetchHealth, {
    refreshInterval: 15_000,
  });
}

function isAnyJobStalled(health: HealthResponse): boolean {
  const stalledMs = 30 * 60 * 1000;
  const now = Date.now();
  const job = health.active_refresh_job;
  if (!job?.started_at) {
    return false;
  }
  return now - new Date(job.started_at).getTime() > stalledMs;
}

export function getSyncState(health?: HealthResponse | null): SyncState | null {
  if (!health) return null;
  const isRefreshing = health.refresh_status === "queued" || health.refresh_status === "running";

  if (isRefreshing) {
    return isAnyJobStalled(health) ? "stalled" : "refreshing";
  }
  if (health.refresh_status === "failed" && health.data_stale) {
    return "failed";
  }
  if (health.data_stale) {
    return "stale";
  }
  return "synced";
}

function fmtRelativeCompact(iso: string | null | undefined): string {
  const formatted = fmtRelative(iso);
  return formatted
    .replace(/^about /, "")
    .replace(/ seconds? ago$/, "s ago")
    .replace(/ minutes? ago$/, "m ago")
    .replace(/ hours? ago$/, "h ago")
    .replace(/ days? ago$/, "d ago")
    .replace(/ months? ago$/, "mo ago")
    .replace(/ years? ago$/, "y ago")
    .replace(/^in (\d+) seconds?$/, "in $1s")
    .replace(/^in (\d+) minutes?$/, "in $1m")
    .replace(/^in (\d+) hours?$/, "in $1h")
    .replace(/^in (\d+) days?$/, "in $1d")
    .replace(/^in (\d+) months?$/, "in $1mo")
    .replace(/^in (\d+) years?$/, "in $1y");
}

export function getMarketSyncBadge(health?: HealthResponse | null): SyncBadge | null {
  if (!health) return null;

  if (health.refresh_status === "queued" || health.refresh_status === "running") {
    return {
      text: health.active_refresh_job?.scope === "current_slate" ? "Market refreshing slate" : "Market refreshing",
      title:
        health.active_refresh_job?.scope === "current_slate"
          ? "Current-slate market refresh is queued or running."
          : "Market refresh is queued or running.",
      variant: "warning",
    };
  }

  if (health.refresh_status === "failed" && health.data_stale) {
    const relative = health.last_successful_refresh_at ? fmtRelativeCompact(health.last_successful_refresh_at) : null;
    return {
      text: relative ? `Market failed ${relative}` : "Market failed",
      title: health.last_successful_refresh_at
        ? `Market refresh failed; last success ${fmtRelative(health.last_successful_refresh_at)}.`
        : "Market refresh failed before the first successful sync.",
      variant: "negative",
    };
  }

  if (health.data_stale) {
    const relative = health.last_successful_refresh_at ? fmtRelativeCompact(health.last_successful_refresh_at) : null;
    return {
      text: relative ? `Market stale ${relative}` : "Market stale",
      title: health.last_successful_refresh_at
        ? `Market data is stale; last success ${fmtRelative(health.last_successful_refresh_at)}.`
        : "Market data is stale and awaiting the first successful refresh.",
      variant: "warning",
    };
  }

  if (health.last_successful_refresh_at) {
    return {
      text: `Market synced ${fmtRelativeCompact(health.last_successful_refresh_at)}`,
      title: `Market data last synced ${fmtRelative(health.last_successful_refresh_at)}.`,
      variant: "positive",
    };
  }

  return {
    text: "Market awaiting refresh",
    title: "Market data is waiting for the first successful refresh.",
    variant: "warning",
  };
}

export function getPropSyncBadge(health?: HealthResponse | null): SyncBadge | null {
  if (!health) return null;

  if (health.prop_refresh_status === "queued" || health.prop_refresh_status === "running") {
    return {
      text: "Maintenance refreshing",
      title: "Maintenance refresh is queued or running.",
      variant: "warning",
    };
  }

  if (health.prop_refresh_status === "failed" && health.prop_data_stale) {
    const relative = health.last_prop_refresh_at ? fmtRelativeCompact(health.last_prop_refresh_at) : null;
    return {
      text: relative ? `Maintenance failed ${relative}` : "Maintenance failed",
      title: health.last_prop_refresh_at
        ? `Maintenance refresh failed; last success ${fmtRelative(health.last_prop_refresh_at)}.`
        : "Maintenance refresh failed before the first successful sync.",
      variant: "negative",
    };
  }

  if (health.prop_data_stale) {
    const relative = health.last_prop_refresh_at ? fmtRelativeCompact(health.last_prop_refresh_at) : null;
    return {
      text: relative ? `Maintenance stale ${relative}` : "Maintenance stale",
      title: health.last_prop_refresh_at
        ? `Maintenance data is stale; last success ${fmtRelative(health.last_prop_refresh_at)}.`
        : "Maintenance data is stale and awaiting the first successful refresh.",
      variant: "warning",
    };
  }

  if (health.last_prop_refresh_at) {
    return {
      text: `Maintenance synced ${fmtRelativeCompact(health.last_prop_refresh_at)}`,
      title: `Maintenance data last synced ${fmtRelative(health.last_prop_refresh_at)}.`,
      variant: "positive",
    };
  }

  return {
    text: "Maintenance awaiting refresh",
    title: "Maintenance data is waiting for the first successful refresh.",
    variant: "warning",
  };
}

function getUserSafeRefreshErrorMessage(message?: string | null) {
  if (!message) {
    return null;
  }

  const trimmed = message.trim();
  const looksTechnical = /(sqlalchemy|sqlite|traceback| at 0x|https?:\/\/|insert into|select |update |delete |database is locked)/i.test(trimmed);
  if (looksTechnical) {
    return null;
  }

  return trimmed.length > 140 ? `${trimmed.slice(0, 137)}...` : trimmed;
}

// Slice 5: ``getFreshnessBanner`` was renamed to ``getOperatorBanner`` to
// match the OperatorBanner / ProductFreshnessBanner split. The semantics
// are the same (it reports on the refresh state machine, which IS an
// operator concern), but the name no longer suggests this is the
// product-facing freshness gauge. Product-facing freshness lives on
// ``/product/freshness`` and on the per-payload ``freshness_status`` field
// of the surfaces themselves.
export function getOperatorBanner(health?: HealthResponse | null) {
  if (!health) return null;
  if (isAnyJobStalled(health)) {
    return {
      tone: "warning" as const,
      message: "A refresh job appears stalled (running over 30 minutes). See Runs for details.",
    };
  }
  const refreshError = getUserSafeRefreshErrorMessage(health.refresh_error_message);
  const propRefreshError = getUserSafeRefreshErrorMessage(health.prop_refresh_error_message);
  const activeRefreshScope = health.active_refresh_job?.scope;

  if (health.refresh_status === "queued" || health.refresh_status === "running") {
    return {
      tone: "neutral" as const,
      message:
        activeRefreshScope === "current_slate"
          ? "Refreshing the current NBA/MLB slate in background."
          : health.prop_refresh_status === "queued" || health.prop_refresh_status === "running"
            ? "Refreshing markets and props in background."
            : "Refreshing market data in background.",
    };
  }
  if (health.refresh_status === "failed") {
    return {
      tone: "warning" as const,
      message: refreshError
        ? `Refresh failed. ${refreshError} See Runs for details.`
        : "Refresh failed. See Runs for details.",
    };
  }
  if (health.prop_refresh_status === "queued" || health.prop_refresh_status === "running") {
    return {
      tone: "neutral" as const,
      message: "Maintenance refresh running in background.",
    };
  }
  if (health.prop_refresh_status === "failed") {
    return {
      tone: "warning" as const,
      message: propRefreshError
        ? `Maintenance refresh failed. ${propRefreshError} See Runs for details.`
        : "Maintenance refresh failed. See Runs for details.",
    };
  }
  return null;
}
