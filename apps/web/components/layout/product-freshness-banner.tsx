"use client";

import useSWR from "swr";
import { fetchProductFreshness, keys } from "@/lib/api";
import type { ProductFreshnessResponse, ProductScopeFreshnessRead } from "@/lib/api";
import { fmtRelative } from "@/lib/utils";

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

function getShortLabel(status: ProductFreshnessResponse["overall_status"]): string {
  switch (status) {
    case "missing":
      return "Awaiting slate";
    case "degraded":
      return "Slate degraded";
    case "empty":
      return "Empty slate";
    default:
      return "Stale slate";
  }
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

  const className =
    data.overall_status === "missing" || data.overall_status === "degraded"
      ? "topbar-chip chip-operator"
      : "topbar-chip chip-product";

  return (
    <span
      className={className}
      role="status"
      data-testid="product-freshness-banner"
      title={message}
    >
      <span className="dot" />
      {getShortLabel(data.overall_status)}
    </span>
  );
}
