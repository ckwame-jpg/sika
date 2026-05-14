"use client";

import { mutate } from "swr";

import { fetchRefreshJob, keys, triggerRefresh } from "@/lib/api";
import type { RefreshJobRead } from "@/lib/types";

const POLL_INTERVAL_MS = 2_500;
const TIMEOUT_MS = 40 * 60_000;

// Bug #35 — historical polls had no AbortSignal. A user clicking
// "refresh" then navigating away (or unmounting the sidebar) left
// the polling loop running for up to 40 minutes, burning network
// + mutating cache for a page they're no longer looking at.
// ``RefreshAbortError`` lets callers distinguish abort from genuine
// failure so the catch site can stay quiet for aborts.
export class RefreshAbortError extends Error {
  constructor(message = "Refresh polling aborted") {
    super(message);
    this.name = "RefreshAbortError";
  }
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new RefreshAbortError());
      return;
    }
    const timeoutId = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timeoutId);
      reject(new RefreshAbortError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

async function waitForRefreshJob(jobId: number, signal?: AbortSignal): Promise<RefreshJobRead> {
  const deadline = Date.now() + TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (signal?.aborted) throw new RefreshAbortError();
    const job = await fetchRefreshJob(jobId);
    await mutate(keys.refreshJob(jobId), job, false);
    if (job.status === "completed" || job.status === "failed") {
      return job;
    }
    await sleep(POLL_INTERVAL_MS, signal);
  }
  throw new Error("Refresh job timed out.");
}

async function revalidateAfterRefresh() {
  await Promise.all([
    mutate((key) => typeof key === "string" && key.startsWith("/ops/runs")),
    mutate((key) => typeof key === "string" && key.startsWith("/events")),
    mutate((key) => typeof key === "string" && key.startsWith("/trade-desk")),
    mutate((key) => typeof key === "string" && key.startsWith("/markets")),
    mutate((key) => typeof key === "string" && key.startsWith("/positions")),
    mutate((key) => typeof key === "string" && key.startsWith("/predictions")),
    mutate((key) => typeof key === "string" && key.startsWith("/parlays")),
    mutate(keys.productFreshness),
    mutate(keys.watchlistDiagnostics),
    mutate(keys.health),
  ]);
}

export async function triggerRefreshAndRevalidate(
  options: { signal?: AbortSignal } = {},
): Promise<RefreshJobRead> {
  const { signal } = options;
  if (signal?.aborted) throw new RefreshAbortError();
  const queued = await triggerRefresh();
  await Promise.all([
    mutate(keys.health),
    mutate(keys.watchlistDiagnostics),
  ]);
  const job = await waitForRefreshJob(queued.job_id, signal);
  await revalidateAfterRefresh();
  if (job.status === "failed") {
    throw new Error(job.error_message || "Refresh failed.");
  }
  return job;
}
