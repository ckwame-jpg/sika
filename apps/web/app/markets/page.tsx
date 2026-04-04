"use client";

import { Suspense, useState } from "react";
import { Header } from "@/components/layout/header";
import { MarketsTable } from "@/components/markets/markets-table";
import { QualityFilterSelect, type RecommendationViewMode } from "@/components/filters/quality-filter-select";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SportFilterSelect, useSportQueryParam } from "@/components/filters/sport-filter-select";
import { sportLabel } from "@/lib/utils";

function MarketsContent() {
  const { sport } = useSportQueryParam();
  const [family, setFamily] = useState("all");
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [qualityMode, setQualityMode] = useState<RecommendationViewMode>("balanced");

  return (
    <div className="flex min-h-full flex-col">
      <div className="border-b border-border bg-surface px-3 py-3 sm:px-5">
        <div className="grid gap-2 sm:flex sm:flex-wrap sm:items-center">
          <div className="flex items-center justify-between gap-2 sm:justify-start">
            <span className="text-xs text-muted-foreground">Sport</span>
            <SportFilterSelect triggerClassName="h-8 w-[min(200px,60vw)] text-xs sm:w-[140px]" />
          </div>
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search ticker or market title"
            className="h-8 w-full sm:w-[280px]"
          />
          <Select value={family} onValueChange={setFamily}>
            <SelectTrigger className="h-8 w-full sm:w-[180px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All families</SelectItem>
              <SelectItem value="player_prop">Player props</SelectItem>
              <SelectItem value="winner">Winners</SelectItem>
            </SelectContent>
          </Select>
          <Select value={status} onValueChange={setStatus}>
            <SelectTrigger className="h-8 w-full sm:w-[140px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              <SelectItem value="active">Active</SelectItem>
              <SelectItem value="open">Open</SelectItem>
              <SelectItem value="settled">Settled</SelectItem>
            </SelectContent>
          </Select>
          <QualityFilterSelect
            value={qualityMode}
            onValueChange={setQualityMode}
            triggerClassName="h-8 w-full sm:w-[130px]"
          />
          <span className="text-xs text-muted-foreground sm:ml-auto">
            {sportLabel(sport)} · 30s refresh
          </span>
        </div>
      </div>
      <div className="p-3 sm:p-4">
        <MarketsTable
          sport={sport}
          family={family === "all" ? undefined : family}
          status={status === "all" ? undefined : status}
          search={search || undefined}
          qualityMode={qualityMode}
        />
      </div>
    </div>
  );
}

export default function MarketsPage() {
  return (
    <>
      <Header
        title="Markets"
        description="Live market tape with pricing, edge, and prop metadata"
      />
      <main className="flex-1 overflow-y-auto">
        <Suspense>
          <MarketsContent />
        </Suspense>
      </main>
    </>
  );
}
