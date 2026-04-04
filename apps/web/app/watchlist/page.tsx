"use client";

import { Suspense, useState } from "react";
import { ViewSwitch, useViewQueryParam } from "@/components/filters/view-switch";
import { QualityFilterSelect, type RecommendationViewMode } from "@/components/filters/quality-filter-select";
import { Header } from "@/components/layout/header";
import { ParlayFilterControls } from "@/components/parlays/parlay-filter-controls";
import { ParlayWatchlistSection } from "@/components/parlays/parlay-watchlist-section";
import { WatchlistTable } from "@/components/watchlist/watchlist-table";
import { EDGE_EXPLANATION } from "@/lib/market-copy";
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
  const [qualityMode, setQualityMode] = useState<RecommendationViewMode>("balanced");
  const [parlaySportScope, setParlaySportScope] = useState("all");
  const [parlayLegCount, setParlayLegCount] = useState("all");

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex flex-col gap-2 border-b border-border bg-surface px-3 py-3 sm:px-5">
        <ViewSwitch view={view} onChange={setView} className="w-fit" />
        {view === "singles" ? (
          <div className="grid gap-2 sm:flex sm:flex-wrap sm:items-center">
            <div className="flex items-center justify-between gap-2 sm:justify-start">
              <span className="text-xs text-muted-foreground">Sport</span>
              <SportFilterSelect triggerClassName="h-8 w-[min(200px,60vw)] text-xs sm:w-[140px]" />
            </div>
            {qualityMode !== "coverage" && (
              <div className="flex items-center justify-between gap-2 sm:justify-start">
                <span className="text-xs text-muted-foreground">Show</span>
                <Select
                  value={String(limit)}
                  onValueChange={(value) => setLimit(Number(value))}
                >
                  <SelectTrigger className="h-8 w-[min(200px,60vw)] text-xs sm:w-24">
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
            )}
            <div className="flex items-center justify-between gap-2 sm:justify-start">
              <span className="text-xs text-muted-foreground">Mode</span>
              <QualityFilterSelect
                value={qualityMode}
                onValueChange={setQualityMode}
                triggerClassName="h-8 w-[min(200px,60vw)] text-xs sm:w-[130px]"
                includeCoverage
              />
            </div>
          </div>
        ) : (
          <ParlayFilterControls
            sportScope={parlaySportScope}
            onSportScopeChange={setParlaySportScope}
            legCount={parlayLegCount}
            onLegCountChange={setParlayLegCount}
          />
        )}
        <span className="hidden text-xs text-muted-foreground lg:ml-auto lg:inline">
          {view === "singles"
            ? qualityMode === "coverage"
              ? "Current-slate order: Tip-off, then winners and props"
              : "Default sort: Edge · Click headers to sort"
            : "Top-ranked combinations"} · 30s refresh
        </span>
        <span className="text-xs text-muted-foreground lg:hidden">
          {view === "singles"
            ? qualityMode === "coverage"
              ? "Current-slate order"
              : "Default sort: Edge"
            : "Top-ranked combinations"} · 30s refresh
        </span>
      </div>

      <div className="border-b border-border bg-surface px-3 py-2 text-xs text-muted-foreground sm:px-5">
        {view === "singles"
          ? qualityMode === "coverage"
            ? "Coverage mode lists current NBA and MLB winner markets plus available player props for today's slate. Recommendation and prediction badges show which rows cleared thresholds versus which are coverage-only."
            : `${EDGE_EXPLANATION} Use Trade to route a single-market pick to paper or demo.`
          : "Synthetic parlays combine the strongest current NBA and MLB single-pick edges. Filter by sport scope and preferred leg count to surface the combinations you actually want to scan."}
      </div>

      <div className="space-y-4 p-3 sm:p-4">
        {view === "singles" ? (
          <WatchlistTable sport={sport} limit={limit} qualityMode={qualityMode} />
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
