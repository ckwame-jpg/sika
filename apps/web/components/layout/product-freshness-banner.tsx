"use client";

// Slice 5: split of the v1 ``FreshnessBanner``. The product half.
//
// This banner reads ``/product/freshness`` — the side-effect-free gauge
// that aggregates the per-scope ``current_slate_snapshots`` rows and
// returns an ``overall_status`` of ``"fresh" | "stale" | "missing"``.
// It is the only banner that speaks the *product* freshness vocabulary;
// the per-surface stale pills (e.g. ``StaleSlatePill`` on the trade
// desk) remain the canonical indicator and this banner is the fallback
// for surfaces that don't yet own a pill of their own.
//
// The banner stays silent when everything is fresh — the product is
// healthy and there's nothing to surface. It also never blocks the
// shell: a ``/product/freshness`` failure simply hides the banner.

import useSWR from "swr";
import { fetchProductFreshness, keys } from "@/lib/api";
import type { ProductFreshnessResponse, ProductScopeFreshnessRead } from "@/lib/api";
import { cn, fmtRelative } from "@/lib/utils";

function pickWorstScope(
  scopes: ProductScopeFreshnessRead[],
): ProductScopeFreshnessRead | null {
  const missing = scopes.find((s) => s.status === "missing");
  if (missing) return missing;
  const degraded = scopes.find((s) => s.status === "degraded");
  if (degraded) return degraded;
  const stale = scopes.find((s) => s.status === "stale");
  if (stale) return stale;
  const empty = scopes.find((s) => s.status === "empty");
  if (empty) return empty;
  return null;
}

function formatScopeLabel(scope: string): string {
  if (scope === "all") return "the slate";
  return scope;
}

function getMessage(data: ProductFreshnessResponse): string | null {
  if (data.overall_status === "fresh") return null;

  const worst = pickWorstScope(data.scopes ?? []);

  if (data.overall_status === "missing") {
    if (worst && worst.scope !== "all") {
      return `${formatScopeLabel(worst.scope)} data is awaiting its first refresh.`;
    }
    return "Product data is awaiting its first refresh.";
  }

  if (data.overall_status === "degraded") {
    return worst?.blocking_reason || "Product data is degraded.";
  }

  if (data.overall_status === "empty") {
    return worst?.blocking_reason || "Current slate has no trade-ready markets.";
  }

  if (worst && worst.generated_at) {
    const relative = fmtRelative(worst.generated_at);
    if (worst.scope !== "all") {
      return `${formatScopeLabel(worst.scope)} data last refreshed ${relative}.`;
    }
    return `Product data last refreshed ${relative}.`;
  }
  return "Product data is stale.";
}

export function ProductFreshnessBanner() {
  const { data } = useSWR<ProductFreshnessResponse>(
    keys.productFreshness,
    fetchProductFreshness,
    { refreshInterval: 30_000 },
  );

  if (!data) return null;
  const message = getMessage(data);
  if (!message) return null;

  const tone = data.overall_status === "missing" || data.overall_status === "degraded" ? "warning" : "muted";

  return (
    <div
      className={cn(
        "border-b px-3 py-2 text-xs sm:px-5",
        tone === "warning"
          ? "border-warning/20 bg-warning/10 text-warning"
          : "border-border bg-surface text-muted-foreground",
      )}
      role="status"
      data-testid="product-freshness-banner"
    >
      {message}
    </div>
  );
}
