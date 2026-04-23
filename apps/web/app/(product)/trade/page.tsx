"use client";

import { Suspense } from "react";
import { Header } from "@/components/layout/header";
import { SportFilterSelect, useSportQueryParam } from "@/components/filters/sport-filter-select";
import { TradeDesk } from "@/components/trade/trade-desk";

/**
 * Cosmos v2 (phase 3) — Trade page.
 *
 * Only chrome changes here. `TradeDesk` still owns all of the data wiring;
 * this file reskins the sticky filter bar that lives above it.
 */
function TradeContent() {
  const { sport } = useSportQueryParam();

  return (
    <>
      <div
        className={[
          "sticky top-0 z-10",
          "border-b border-white/5",
          "bg-[hsl(250_55%_4%/0.55)] backdrop-blur-[14px]",
          "px-3 py-2.5 sm:px-5",
        ].join(" ")}
      >
        <div className="flex items-center gap-3">
          <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-muted-foreground">
            sport
          </span>
          <SportFilterSelect triggerClassName="h-8 w-[140px] text-xs" />
          <span className="ml-auto inline-flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.1em] text-muted-foreground">
            <span className="cosmos-live-dot" />
            30s refresh
          </span>
        </div>
      </div>
      <div className="p-3 sm:p-5">
        <TradeDesk sport={sport} />
      </div>
    </>
  );
}

export default function TradePage() {
  return (
    <>
      <Header
        title="Trade"
        description="Event-first desk for curated game lines and player props"
      />
      <main className="flex-1 overflow-y-auto">
        <Suspense>
          <TradeContent />
        </Suspense>
      </main>
    </>
  );
}
