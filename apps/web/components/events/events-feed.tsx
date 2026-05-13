"use client";

import useSWR from "swr";
import { fetchEvents, keys } from "@/lib/api";
import type { EventRead, EventParticipantRead } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton, SkeletonRow } from "@/components/ui/skeleton";
import {
  eventStatusLabel,
  filterDashboardEvents,
  filterEventsForDay,
  fmtDate,
  fmtTime,
  isLive,
  sportLabel,
} from "@/lib/utils";

import { sportTint } from "@/lib/sport-tints";  // bug #30 — shared map

function SportPill({ sportKey }: { sportKey: string }) {
  const tint = sportTint(sportKey);
  return (
    <span
      className="sport-pill"
      style={{ ["--tint" as string]: tint }}
    >
      <span className="dot" aria-hidden />
      <span>{sportLabel(sportKey)}</span>
    </span>
  );
}

function StatusPill({ status }: { status: string }) {
  const label = eventStatusLabel(status);
  if (isLive(status)) {
    return (
      <span className="event-status-pill live">
        <span className="live-dot" aria-hidden />
        <span>{label}</span>
      </span>
    );
  }
  if (status === "completed") {
    return <span className="event-status-pill final">{label}</span>;
  }
  return <span className="event-status-pill scheduled">{label}</span>;
}

function ParticipantScore({
  home,
  away,
}: {
  home: EventParticipantRead | undefined;
  away: EventParticipantRead | undefined;
}) {
  const hasScore = home?.score != null || away?.score != null;
  if (!hasScore) {
    return <span className="font-mono text-xs text-muted-foreground">—</span>;
  }
  return (
    <span className="font-mono text-[13px] font-semibold tabular-nums text-foreground tracking-[0.02em]">
      {away?.score ?? "—"} – {home?.score ?? "—"}
    </span>
  );
}

function EventRow({ event }: { event: EventRead }) {
  const home = event.participants.find((participant) => participant.is_home);
  const away = event.participants.find((participant) => !participant.is_home);
  const live = isLive(event.status);

  return (
    <TableRow className={live ? "event-row-live" : undefined}>
      <TableCell>
        <SportPill sportKey={event.sport_key} />
      </TableCell>
      <TableCell>
        <div className="flex flex-col gap-0.5">
          <span className="text-[13.5px] font-medium text-foreground tracking-[-0.005em]">
            {event.name}
          </span>
          <span className="font-mono text-[11px] text-muted-foreground">
            {away?.display_name ?? "—"} @ {home?.display_name ?? "—"}
          </span>
        </div>
      </TableCell>
      <TableCell className="text-right">
        <ParticipantScore home={home} away={away} />
      </TableCell>
      <TableCell>
        <StatusPill status={event.status} />
      </TableCell>
      <TableCell className="text-right">
        <div className={`font-mono text-xs ${live ? "text-positive" : "text-foreground"}`}>
          {fmtDate(event.starts_at)}
        </div>
        <div className="font-mono text-[11px] text-muted-foreground">
          {fmtTime(event.starts_at)}
        </div>
      </TableCell>
    </TableRow>
  );
}

function EventCard({ event }: { event: EventRead }) {
  const home = event.participants.find((participant) => participant.is_home);
  const away = event.participants.find((participant) => !participant.is_home);
  const live = isLive(event.status);
  const tint = sportTint(event.sport_key);
  const hasScore = home?.score != null || away?.score != null;

  return (
    <article
      className="event-card"
      style={{ ["--sport-tint" as string]: tint }}
    >
      <header className="event-card-head">
        <SportPill sportKey={event.sport_key} />
        <StatusPill status={event.status} />
        <span className="event-card-when">{fmtTime(event.starts_at)}</span>
      </header>
      <div className="event-card-body">
        <div className="text-sm font-semibold text-foreground tracking-[-0.01em]">
          {event.name}
        </div>
        <div className="mt-[3px] font-mono text-[11.5px] text-muted-foreground">
          {away?.display_name ?? "—"} @ {home?.display_name ?? "—"}
        </div>
      </div>
      <div className="event-card-grid">
        <div className="event-card-tile">
          <div className="event-card-tile-label">Score</div>
          <div
            className={`event-card-tile-value ${hasScore ? "" : "muted"}`}
          >
            {hasScore ? `${away?.score ?? "—"} – ${home?.score ?? "—"}` : "—"}
          </div>
        </div>
        <div className="event-card-tile">
          <div className="event-card-tile-label">Starts</div>
          <div
            className={`event-card-tile-value ${live ? "live" : ""}`}
          >
            {fmtDate(event.starts_at)}
          </div>
          <div className="event-card-tile-sub">{fmtTime(event.starts_at)}</div>
        </div>
      </div>
    </article>
  );
}

interface EventsFeedProps {
  sport?: string;
  day?: string;
  compact?: boolean;
  mode?: "dashboard" | "day";
}

export function EventsFeed({
  sport,
  day,
  compact,
  mode = "day",
}: EventsFeedProps) {
  const { data, isLoading, error } = useSWR<EventRead[]>(
    keys.events(sport),
    () => fetchEvents(sport),
    { refreshInterval: 30_000 },
  );

  if (error) {
    return (
      <div className="rounded-xl border border-negative/30 bg-negative-dim px-4 py-8 text-center">
        <div className="mx-auto flex h-2 w-2 items-center justify-center">
          <span className="h-2 w-2 rounded-full bg-negative shadow-[0_0_8px_0_var(--negative)]" />
        </div>
        <p className="mt-3 text-sm font-medium text-foreground">Couldn&rsquo;t reach the events feed.</p>
        <p className="mt-1 text-xs text-muted-foreground">
          The API didn&rsquo;t respond. Check that the backend is running, then try again.
        </p>
      </div>
    );
  }

  const events = data ?? [];
  const filtered = mode === "dashboard"
    ? filterDashboardEvents(events)
    : day
      ? filterEventsForDay(events, day)
      : filterDashboardEvents(events);
  const emptyMessage = mode === "dashboard"
    ? "No live or upcoming events found."
    : "No events matched the selected date.";

  return (
    <>
      <div className="flex flex-col gap-3 lg:hidden">
        {isLoading
          ? Array.from({ length: compact ? 4 : 6 }).map((_, index) => (
              <div key={index} className="event-card">
                <div className="event-card-head">
                  <Skeleton className="h-5 w-16" />
                  <Skeleton className="h-5 w-14" />
                </div>
                <div className="event-card-body">
                  <Skeleton className="h-4 w-3/4" />
                  <Skeleton className="mt-2 h-3 w-1/2" />
                </div>
                <div className="event-card-grid">
                  <Skeleton className="h-14 w-full" />
                  <Skeleton className="h-14 w-full" />
                </div>
              </div>
            ))
          : filtered.length === 0
            ? (
              <div className="event-card-empty">{emptyMessage}</div>
            )
            : filtered.map((event) => <EventCard key={event.id} event={event} />)}
      </div>

      <div className="hidden lg:block">
        <div className="cosmos-table-wrap">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-20">Sport</TableHead>
                <TableHead>Event</TableHead>
                <TableHead className="w-20 text-right">Score</TableHead>
                <TableHead className="w-28">Status</TableHead>
                <TableHead className="w-32 text-right">Starts</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading
                ? Array.from({ length: compact ? 5 : 8 }).map((_, index) => (
                    <SkeletonRow key={index} cols={5} />
                  ))
                : filtered.length === 0
                  ? (
                    <TableRow>
                      <TableCell colSpan={5} className="cosmos-table-empty">
                        {emptyMessage}
                      </TableCell>
                    </TableRow>
                  )
                  : filtered.map((event) => <EventRow key={event.id} event={event} />)}
            </TableBody>
          </Table>
        </div>
      </div>
    </>
  );
}
