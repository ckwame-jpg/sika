"use client";

import { getOperatorBanner, useHealthStatus } from "@/lib/health-status";

export function OperatorBanner() {
  const { data: health } = useHealthStatus();
  const banner = getOperatorBanner(health);

  if (!banner) return null;

  const baseClass =
    banner.tone === "warning"
      ? "topbar-chip chip-operator"
      : "topbar-chip chip-product";
  const className = banner.active ? `${baseClass} chip-refreshing` : baseClass;

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
