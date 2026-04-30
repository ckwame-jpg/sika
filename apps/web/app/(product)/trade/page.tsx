"use client";

import { Suspense } from "react";
import { Header } from "@/components/layout/header";
import { SportFilterSelect, useSportQueryParam } from "@/components/filters/sport-filter-select";
import { TradeDesk } from "@/components/trade/trade-desk";

function TradeContent() {
  const { sport } = useSportQueryParam();

  return (
    <>
      <div className="border-b border-border bg-surface px-3 py-3 sm:px-5">
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">Sport</span>
          <SportFilterSelect triggerClassName="h-8 w-[140px] text-xs" />
          <span className="ml-auto text-xs text-muted-foreground">30s refresh</span>
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
      <main className="flex-1 overflow-visible">
        <Suspense>
          <TradeContent />
        </Suspense>
      </main>
    </>
  );
}
