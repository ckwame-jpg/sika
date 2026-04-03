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

export function getFreshnessBanner(health?: HealthResponse | null) {
  if (!health) return null;
  if (health.refresh_status === "queued" || health.refresh_status === "running") {
    return {
      tone: "neutral" as const,
      message: "Refreshing data in background; cached data may be shown briefly.",
    };
  }
  if (health.refresh_status === "failed" && health.data_stale) {
    return {
      tone: "warning" as const,
      message: health.refresh_error_message
        ? `Refresh failed; cached data may be stale. ${health.refresh_error_message}`
        : "Refresh failed; cached data may be stale until the next retry.",
    };
  }
  return null;
}
