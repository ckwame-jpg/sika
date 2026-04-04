"use client";

import { getFreshnessBanner, useHealthStatus } from "@/lib/health-status";
import { cn } from "@/lib/utils";

export function FreshnessBanner() {
  const { data: health } = useHealthStatus();
  const banner = getFreshnessBanner(health);

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
    >
      {banner.message}
    </div>
  );
}
