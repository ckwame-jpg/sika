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
    <div className="flex min-h-full flex-col gap-4 p-3 sm:p-4">
      <div className="cosmos-toolbar">
        <SportFilterSelect triggerClassName="h-8 w-full text-xs sm:w-[140px]" />
        <Input
          type="date"
          className="h-8 w-full text-xs sm:w-40"
          value={day}
          onChange={(event) => setDay(event.target.value || today)}
        />
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setDay(today)}
          disabled={day === today}
        >
          Today
        </Button>
        <div className="cosmos-toolbar-spacer">
          <span className="cosmos-toolbar-meta">
            Local date filter · 30s refresh
          </span>
        </div>
      </div>

      <EventsFeed sport={sport} day={day} mode="day" />
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
