"use client";

import { getOperatorBanner, useHealthStatus } from "@/lib/health-status";

export function OperatorBanner() {
  const { data: health } = useHealthStatus();
  const banner = getOperatorBanner(health);

  if (!banner) return null;

  const className =
    banner.tone === "warning"
      ? "topbar-chip chip-operator"
      : "topbar-chip chip-product";

  return (
    <span
      className={className}
      role="status"
      data-testid="operator-banner"
      title={banner.message}
    >
      <span className="dot" />
      {banner.tone === "warning" ? "Refresh issue" : "Refresh"}
    </span>
  );
}
