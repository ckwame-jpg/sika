"use client";

import { Sidebar } from "./sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Suspense } from "react";
import { OperatorBanner } from "@/components/layout/operator-banner";
import { ProductFreshnessBanner } from "@/components/layout/product-freshness-banner";

// Slice 3: ``ApiBoundary`` was deleted. The v1 shell wrapped its children in
// a global ``useHealthStatus``-backed gate that blanked the ENTIRE app on any
// transient ``/health`` failure — including surfaces with perfectly fresh SWR
// caches and surfaces that don't depend on live data at all (runs, history,
// docs). That is a product failure mode, not a defensive feature.
//
// Slice 5: the v1 ``FreshnessBanner`` has been split into two banners that
// each speak to a single audience.
//
//   * ``OperatorBanner`` reads ``/health`` and surfaces operator-scoped state
//     — refresh stalled, refresh failed, maintenance refresh running. It
//     never says "data is stale"; product staleness lives elsewhere.
//   * ``ProductFreshnessBanner`` reads ``/product/freshness`` (the
//     side-effect-free gauge over ``current_slate_snapshots``) and shows a
//     product-wide stale notice when ``overall_status !== "fresh"``. It is a
//     fallback for surfaces that don't yet own a per-surface stale pill.
//
// Per-surface components remain authoritative for their own freshness via
// the ``freshness_status`` field on their payload (e.g. ``StaleSlatePill``
// on the trade desk). The two banners stack so operator and product audiences
// each get their own line without conflating concerns.
export function Shell({ children }: { children: React.ReactNode }) {
  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-screen w-full overflow-hidden bg-[radial-gradient(circle_at_top_left,rgba(59,92,170,0.16),transparent_22%),radial-gradient(circle_at_bottom_right,rgba(154,120,57,0.12),transparent_24%),var(--background)]">
        <Suspense>
          <Sidebar />
        </Suspense>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          <OperatorBanner />
          <ProductFreshnessBanner />
          {children}
        </div>
      </div>
    </TooltipProvider>
  );
}
