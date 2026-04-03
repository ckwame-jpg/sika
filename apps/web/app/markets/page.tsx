"use client";

import { Suspense, useState } from "react";
import { Header } from "@/components/layout/header";
import { MarketsTable } from "@/components/markets/markets-table";
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

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-border bg-surface px-5 py-3">
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Sport</span>
          <SportFilterSelect triggerClassName="h-8 w-[140px] text-xs" />
        </div>
        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search ticker or market title"
          className="h-8 w-[280px]"
        />
        <Select value={family} onValueChange={setFamily}>
          <SelectTrigger className="h-8 w-[180px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All families</SelectItem>
            <SelectItem value="player_prop">Player props</SelectItem>
            <SelectItem value="winner">Winners</SelectItem>
          </SelectContent>
        </Select>
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger className="h-8 w-[140px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="active">Active</SelectItem>
            <SelectItem value="open">Open</SelectItem>
            <SelectItem value="settled">Settled</SelectItem>
          </SelectContent>
        </Select>
        <span className="ml-auto text-xs text-muted-foreground">
          {sportLabel(sport)} · 30s refresh
        </span>
      </div>
      <div className="p-4">
        <MarketsTable
          sport={sport}
          family={family === "all" ? undefined : family}
          status={status === "all" ? undefined : status}
          search={search || undefined}
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
