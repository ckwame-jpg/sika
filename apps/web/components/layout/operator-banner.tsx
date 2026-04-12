"use client";

// Slice 5: split of the v1 ``FreshnessBanner``. The v1 component was
// dual-purpose — it spoke both *operator* state ("refresh stalled",
// "refresh failed", "maintenance refreshing in background") and
// *product* state ("data is stale") through a single ``/health`` poll.
// That conflated two audiences. The product audience already learns
// freshness from the ``freshness_status`` field on the trade-desk
// payload (Slice 0) and from ``/product/freshness`` (Slice 3). The
// operator audience still needs ``/health`` to surface refresh
// failures that are not yet "data is stale" but soon will be.
//
// This component is the operator-only half. It reads ``/health`` and
// shows operator-scoped messages. It says nothing about product data
// freshness — that is the ``ProductFreshnessBanner``'s job.

import { getOperatorBanner, useHealthStatus } from "@/lib/health-status";
import { cn } from "@/lib/utils";

export function OperatorBanner() {
  const { data: health } = useHealthStatus();
  const banner = getOperatorBanner(health);

  if (!banner) {
    return null;
  }

  return (
    <div
      className={cn(
        "border-b px-3 py-2 text-xs sm:px-5",
        banner.tone === "warning"
          ? "border-warning/20 bg-warning/10 text-warning"
          : "border-border bg-surface text-muted-foreground",
      )}
      role="status"
      data-testid="operator-banner"
    >
      {banner.message}
    </div>
  );
}
