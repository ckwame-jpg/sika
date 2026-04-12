"use client";

import { mutate } from "swr";

import { fetchRefreshJob, keys, triggerRefresh } from "@/lib/api";
import type { RefreshJobRead } from "@/lib/types";

const POLL_INTERVAL_MS = 2_500;
const TIMEOUT_MS = 40 * 60_000;

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForRefreshJob(jobId: number): Promise<RefreshJobRead> {
  const deadline = Date.now() + TIMEOUT_MS;
  while (Date.now() < deadline) {
    const job = await fetchRefreshJob(jobId);
    await mutate(keys.refreshJob(jobId), job, false);
    if (job.status === "completed" || job.status === "failed") {
      return job;
    }
    await sleep(POLL_INTERVAL_MS);
  }
  throw new Error("Refresh job timed out.");
}

async function revalidateAfterRefresh() {
  await Promise.all([
    mutate((key) => typeof key === "string" && key.startsWith("/ops/runs")),
    mutate((key) => typeof key === "string" && key.startsWith("/events")),
    mutate((key) => typeof key === "string" && key.startsWith("/watchlist")),
    mutate((key) => typeof key === "string" && key.startsWith("/markets")),
    mutate((key) => typeof key === "string" && key.startsWith("/positions")),
    mutate((key) => typeof key === "string" && key.startsWith("/predictions")),
    mutate((key) => typeof key === "string" && key.startsWith("/parlays")),
    mutate(keys.watchlistDiagnostics),
    mutate(keys.health),
  ]);
}

export async function triggerRefreshAndRevalidate(): Promise<RefreshJobRead> {
  const queued = await triggerRefresh();
  await Promise.all([
    mutate(keys.health),
    mutate(keys.watchlistDiagnostics),
  ]);
  const job = await waitForRefreshJob(queued.job_id);
  await revalidateAfterRefresh();
  if (job.status === "failed") {
    throw new Error(job.error_message || "Refresh failed.");
  }
  return job;
}
