"use client";

import useSWR from "swr";
import { fetchHealth, keys } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";

export type SyncState = "refreshing" | "failed" | "stale" | "synced";

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
