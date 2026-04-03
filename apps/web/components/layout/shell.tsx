"use client";

import { Sidebar } from "./sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Suspense } from "react";
import { StatsAssistant } from "@/components/stats/stats-assistant";
import { FreshnessBanner } from "@/components/layout/freshness-banner";

export function Shell({ children }: { children: React.ReactNode }) {
  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-screen w-full overflow-hidden bg-[radial-gradient(circle_at_top_left,rgba(59,92,170,0.16),transparent_22%),radial-gradient(circle_at_bottom_right,rgba(154,120,57,0.12),transparent_24%),var(--background)]">
        <Suspense>
          <Sidebar />
        </Suspense>
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <FreshnessBanner />
          {children}
        </div>
        <StatsAssistant />
      </div>
    </TooltipProvider>
  );
}
