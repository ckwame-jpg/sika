"use client";

import { Suspense, useState } from "react";
import { Header } from "@/components/layout/header";
import { EventsFeed } from "@/components/events/events-feed";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { SportFilterSelect, useSportQueryParam } from "@/components/filters/sport-filter-select";
import { getLocalDateInputValue } from "@/lib/utils";

function EventsContent() {
  const { sport } = useSportQueryParam();
  const today = getLocalDateInputValue();
  const [day, setDay] = useState<string>(today);

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex flex-col gap-2 border-b border-border bg-surface px-3 py-3 sm:px-5">
        <div className="flex items-center justify-between gap-2 sm:justify-start">
          <span className="text-xs text-muted-foreground">Sport</span>
          <SportFilterSelect triggerClassName="h-8 w-[min(200px,60vw)] text-xs sm:w-[140px]" />
        </div>
        <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground sm:justify-start">
          <span>Date</span>
          <Input
            type="date"
            className="h-8 w-[min(200px,60vw)] text-xs sm:w-40"
            value={day}
            onChange={(event) => setDay(event.target.value || today)}
          />
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setDay(today)}
          disabled={day === today}
        >
          Today
        </Button>
        <span className="text-xs text-muted-foreground lg:ml-auto">
          Local date filter · 30s refresh
        </span>
      </div>

      <div className="p-3 sm:p-4">
        <EventsFeed sport={sport} day={day} mode="day" />
      </div>
    </div>
  );
}

export default function EventsPage() {
  return (
    <>
      <Header
        title="Events"
        description="Sporting events mapped to Kalshi markets"
      />
      <main className="flex-1 overflow-y-auto">
        <Suspense>
          <EventsContent />
        </Suspense>
      </main>
    </>
  );
}
