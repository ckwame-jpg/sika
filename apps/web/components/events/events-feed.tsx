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
import { Badge, SportBadge } from "@/components/ui/badge";
import { SkeletonRow } from "@/components/ui/skeleton";
import {
  eventStatusLabel,
  filterDashboardEvents,
  filterEventsForDay,
  fmtDate,
  fmtTime,
  isLive,
} from "@/lib/utils";

function ParticipantScore({
  home,
  away,
}: {
  home: EventParticipantRead | undefined;
  away: EventParticipantRead | undefined;
}) {
  if (!home && !away) return <span className="text-muted-foreground">—</span>;
  const hasScore = home?.score != null || away?.score != null;
  if (!hasScore) return <span className="text-muted-foreground">—</span>;

  return (
    <span className="font-mono text-xs tabular-nums text-foreground">
      {home?.score ?? "—"} – {away?.score ?? "—"}
    </span>
  );
}

function EventRow({ event }: { event: EventRead }) {
  const home = event.participants.find((participant) => participant.is_home);
  const away = event.participants.find((participant) => !participant.is_home);
  const live = isLive(event.status);

  return (
    <TableRow>
      <TableCell>
        <SportBadge sport={event.sport_key} />
      </TableCell>
      <TableCell>
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-medium text-foreground">{event.name}</span>
          <span className="text-xs text-muted-foreground">
            {away?.display_name} @ {home?.display_name}
          </span>
        </div>
      </TableCell>
      <TableCell>
        <ParticipantScore home={home} away={away} />
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1.5">
          {live && <span className="live-dot" />}
          <Badge variant={live ? "positive" : "default"}>
            {eventStatusLabel(event.status)}
          </Badge>
        </div>
      </TableCell>
      <TableCell>
        <div className="font-mono text-xs text-muted-foreground">
          <div>{fmtDate(event.starts_at)}</div>
          <div>{fmtTime(event.starts_at)}</div>
        </div>
      </TableCell>
    </TableRow>
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
      <div className="flex h-24 items-center justify-center text-xs text-negative">
        Failed to load events. Is the API running?
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
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-20">Sport</TableHead>
            <TableHead>Event</TableHead>
            <TableHead className="w-20">Score</TableHead>
            <TableHead className="w-28">Status</TableHead>
            <TableHead className="w-28">Starts</TableHead>
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
                  <TableCell colSpan={5} className="py-8 text-center text-xs text-muted-foreground">
                    {emptyMessage}
                  </TableCell>
                </TableRow>
              )
              : filtered.map((event) => <EventRow key={event.id} event={event} />)}
        </TableBody>
      </Table>
    </div>
  );
}
