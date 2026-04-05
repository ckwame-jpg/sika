"use client";

import useSWR from "swr";
import { fetchHealth, keys } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";
import { fmtRelative } from "@/lib/utils";

export type SyncState = "refreshing" | "failed" | "stale" | "synced";
export type SyncBadgeVariant = "positive" | "warning" | "negative";

export interface SyncBadge {
  text: string;
  title: string;
  variant: SyncBadgeVariant;
}

export function useHealthStatus() {
  return useSWR<HealthResponse>(keys.health, fetchHealth, {
    refreshInterval: 15_000,
  });
}

export function getSyncState(health?: HealthResponse | null): SyncState | null {
  if (!health) return null;
  if (health.refresh_status === "queued" || health.refresh_status === "running") {
    return "refreshing";
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
      text: "Props refreshing",
      title: "Prop refresh is queued or running.",
      variant: "warning",
    };
  }

  if (health.prop_refresh_status === "failed" && health.prop_data_stale) {
    const relative = health.last_prop_refresh_at ? fmtRelativeCompact(health.last_prop_refresh_at) : null;
    return {
      text: relative ? `Props failed ${relative}` : "Props failed",
      title: health.last_prop_refresh_at
        ? `Prop refresh failed; last success ${fmtRelative(health.last_prop_refresh_at)}.`
        : "Prop refresh failed before the first successful sync.",
      variant: "negative",
    };
  }

  if (health.prop_data_stale) {
    const relative = health.last_prop_refresh_at ? fmtRelativeCompact(health.last_prop_refresh_at) : null;
    return {
      text: relative ? `Props stale ${relative}` : "Props stale",
      title: health.last_prop_refresh_at
        ? `Prop data is stale; last success ${fmtRelative(health.last_prop_refresh_at)}.`
        : "Prop data is stale and awaiting the first successful refresh.",
      variant: "warning",
    };
  }

  if (health.last_prop_refresh_at) {
    return {
      text: `Props synced ${fmtRelativeCompact(health.last_prop_refresh_at)}`,
      title: `Prop data last synced ${fmtRelative(health.last_prop_refresh_at)}.`,
      variant: "positive",
    };
  }

  return {
    text: "Props awaiting refresh",
    title: "Prop data is waiting for the first successful refresh.",
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

export function getFreshnessBanner(health?: HealthResponse | null) {
  if (!health) return null;
  const refreshError = getUserSafeRefreshErrorMessage(health.refresh_error_message);
  const propRefreshError = getUserSafeRefreshErrorMessage(health.prop_refresh_error_message);
  const activeRefreshScope = health.active_refresh_job?.scope;

  if (health.refresh_status === "queued" || health.refresh_status === "running") {
    return {
      tone: "neutral" as const,
      message:
        activeRefreshScope === "current_slate"
          ? "Refreshing the current NBA/MLB slate in background; cached data may be shown briefly."
          : health.prop_refresh_status === "queued" || health.prop_refresh_status === "running"
            ? "Refreshing markets and props in background; cached data may be shown briefly."
            : "Refreshing market data in background; cached data may be shown briefly.",
    };
  }
  if (health.refresh_status === "failed" && health.data_stale) {
    return {
      tone: "warning" as const,
      message: refreshError
        ? `Refresh failed; cached data may be stale. ${refreshError} See Runs for details.`
        : "Refresh failed; cached data may be stale until the next retry. See Runs for details.",
    };
  }
  if (health.prop_refresh_status === "queued" || health.prop_refresh_status === "running") {
    return {
      tone: "neutral" as const,
      message: "Markets are synced; prop context is refreshing in background.",
    };
  }
  if (health.prop_data_stale) {
    return {
      tone: "warning" as const,
      message: propRefreshError
        ? `Markets are synced, but prop context is stale. ${propRefreshError} See Runs for details.`
        : "Markets are synced, but prop context is stale while the next prop refresh catches up.",
    };
  }
  return null;
}
