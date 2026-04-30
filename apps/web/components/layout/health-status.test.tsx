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
});
