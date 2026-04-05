"use client";

import { Suspense } from "react";
import { Header } from "@/components/layout/header";
import { TradeDesk } from "@/components/trade/trade-desk";
import { SportFilterSelect, useSportQueryParam } from "@/components/filters/sport-filter-select";

function TradeContent() {
  const { sport } = useSportQueryParam();

  return (
    <div className="flex min-h-full flex-col">
      <div className="border-b border-border bg-surface px-3 py-3 sm:px-5">
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">Sport</span>
          <SportFilterSelect triggerClassName="h-8 w-[140px] text-xs" />
          <span className="ml-auto text-xs text-muted-foreground">
            30s refresh
          </span>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-3 sm:p-5">
        <TradeDesk sport={sport} />
      </div>
    </div>
  );
}

export default function TradePage() {
  return (
    <>
      <Header
        title="Trade"
        description="Live markets grouped by event"
      />
      <main className="flex-1 overflow-y-auto">
        <Suspense>
          <TradeContent />
        </Suspense>
      </main>
    </>
  );
}
