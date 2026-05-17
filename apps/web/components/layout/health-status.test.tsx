import { describe, expect, it } from "vitest";
import { healthFixture } from "@/test/fixtures/trade-fixtures";
import { getMarketSyncBadge, getOperatorBanner, getSyncState } from "@/lib/health-status";

describe("health-status", () => {
  it("distinguishes queued refreshes from worker-offline queued refreshes", () => {
    const queuedHealth = {
      ...healthFixture,
      scheduler_enabled: true,
      refresh_status: "queued" as const,
      active_refresh_job: {
        id: 1072,
        kind: "refresh",
        scope: "current_slate",
        reason: "manual",
        status: "queued" as const,
        run_id: null,
        error_message: null,
        details: {},
        queued_at: "2026-04-30T01:52:07Z",
        started_at: null,
        finished_at: null,
      },
    };

    expect(getSyncState(queuedHealth)).toBe("queued");
    expect(getMarketSyncBadge(queuedHealth)?.text).toBe("Market refresh queued");

    const workerOfflineHealth = {
      ...queuedHealth,
      scheduler_enabled: false,
    };

    expect(getSyncState(workerOfflineHealth)).toBe("worker_offline");
    expect(getMarketSyncBadge(workerOfflineHealth)?.text).toBe("Market queued - worker not running");
    expect(getOperatorBanner(workerOfflineHealth)?.message).toContain("worker is not running");
  });

  // Smarter WNBA PR 8 — regression. The current-slate refresh banner
  // copy names the sports it covers; the day we add WNBA to
  // enabled_sports the copy should mention WNBA. Pin the substring on
  // BOTH the queued and running branches so a future copy rewrite
  // can't silently drop WNBA from either operator-facing message.
  it("mentions NBA, MLB, and WNBA in the queued current-slate refresh banner", () => {
    const queuedSlateHealth = {
      ...healthFixture,
      scheduler_enabled: true,
      refresh_status: "queued" as const,
      active_refresh_job: {
        id: 2042,
        kind: "refresh",
        scope: "current_slate",
        reason: "interval",
        status: "queued" as const,
        run_id: null,
        error_message: null,
        details: {},
        queued_at: "2026-05-17T01:00:00Z",
        started_at: null,
        finished_at: null,
      },
    };

    const message = getOperatorBanner(queuedSlateHealth)?.message ?? "";
    expect(message).toContain("NBA");
    expect(message).toContain("MLB");
    expect(message).toContain("WNBA");
  });

  it("mentions NBA, MLB, and WNBA in the running current-slate refresh banner", () => {
    // ``started_at: null`` short-circuits ``isAnyJobStalled`` (the
    // guard at health-status.ts:27 returns false on missing
    // started_at) so this exercises the "running but not stalled"
    // branch without depending on a mocked clock.
    const runningSlateHealth = {
      ...healthFixture,
      scheduler_enabled: true,
      refresh_status: "running" as const,
      active_refresh_job: {
        id: 2043,
        kind: "refresh",
        scope: "current_slate",
        reason: "interval",
        status: "running" as const,
        run_id: 42,
        error_message: null,
        details: {},
        queued_at: "2026-05-17T01:00:00Z",
        started_at: null,
        finished_at: null,
      },
    };

    const message = getOperatorBanner(runningSlateHealth)?.message ?? "";
    expect(message).toContain("NBA");
    expect(message).toContain("MLB");
    expect(message).toContain("WNBA");
  });
});
