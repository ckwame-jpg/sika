"use client";

import { Suspense, useState } from "react";
import { ViewSwitch, useViewQueryParam } from "@/components/filters/view-switch";
import { Header } from "@/components/layout/header";
import { ParlayFilterControls } from "@/components/parlays/parlay-filter-controls";
import { ParlayWatchlistSection } from "@/components/parlays/parlay-watchlist-section";
import { WatchlistTable } from "@/components/watchlist/watchlist-table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SportFilterSelect, useSportQueryParam } from "@/components/filters/sport-filter-select";

const LIMITS = [25, 50, 100];

function WatchlistContent() {
  const { sport } = useSportQueryParam();
  const { view, setView } = useViewQueryParam();
  const [limit, setLimit] = useState(50);
  const [parlaySportScope, setParlaySportScope] = useState("all");
  const [parlayLegCount, setParlayLegCount] = useState("all");

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-border bg-surface px-5 py-3">
        <ViewSwitch view={view} onChange={setView} />
        {view === "singles" ? (
          <>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">Sport</span>
              <SportFilterSelect triggerClassName="h-7 w-[140px] text-xs" />
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">Show</span>
              <Select
                value={String(limit)}
                onValueChange={(value) => setLimit(Number(value))}
              >
                <SelectTrigger className="h-7 w-24 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {LIMITS.map((value) => (
                    <SelectItem key={value} value={String(value)}>
                      {value} items
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </>
        ) : (
          <ParlayFilterControls
            sportScope={parlaySportScope}
            onSportScopeChange={setParlaySportScope}
            legCount={parlayLegCount}
            onLegCountChange={setParlayLegCount}
          />
        )}
        <span className="ml-auto text-xs text-muted-foreground">
          {view === "singles" ? "Sorted by edge" : "Top-ranked combinations"} · 30s refresh
        </span>
      </div>

      <div className="border-b border-border bg-surface px-5 py-2 text-xs text-muted-foreground">
        {view === "singles"
          ? "Edge = model fair price minus current suggested market price. Positive edge means the model thinks the price is favorable. Use Trade to route a single-market pick to paper or demo."
          : "Synthetic parlays combine the strongest current NBA and MLB single-pick edges. Filter by sport scope and preferred leg count to surface the combinations you actually want to scan."}
      </div>

      <div className="space-y-4 p-4">
        {view === "singles" ? (
          <WatchlistTable sport={sport} limit={limit} />
        ) : (
          <ParlayWatchlistSection
            sportScope={parlaySportScope}
            legCount={parlayLegCount}
          />
        )}
      </div>
    </div>
  );
}

export default function WatchlistPage() {
  return (
    <>
      <Header
        title="Watchlist"
        description="Recommended trading opportunities sorted by edge"
      />
      <main className="flex-1 overflow-y-auto">
        <Suspense>
          <WatchlistContent />
        </Suspense>
      </main>
    </>
  );
}
