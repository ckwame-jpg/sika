"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { fetchEvents, keys } from "@/lib/api";
import type { EventRead, EventParticipantRead } from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty-state";
import {
  cn,
  eventStatusLabel,
  filterDashboardEvents,
  filterEventsForDay,
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

/** live first, then upcoming (soonest first), then finals (latest first). */
function timelineOrder(events: EventRead[]): EventRead[] {
  const live = events.filter((event) => isLive(event.status));
  const upcoming = events
    .filter((event) => !isLive(event.status) && event.status !== "completed")
    .sort((a, b) => new Date(a.starts_at).getTime() - new Date(b.starts_at).getTime());
  const done = events
    .filter((event) => event.status === "completed")
    .sort((a, b) => new Date(b.starts_at).getTime() - new Date(a.starts_at).getTime());
  return [...live, ...upcoming, ...done];
}

function nodeColor(event: EventRead): string {
  if (isLive(event.status)) return "var(--gi-green)";
  if (event.status === "completed") return "var(--color-cosmos-violet-500)";
  return sportTint(event.sport_key);
}

function TimelineItem({ event }: { event: EventRead }) {
  const home = event.participants.find((participant) => participant.is_home);
  const away = event.participants.find((participant) => !participant.is_home);
  const live = isLive(event.status);
  const done = event.status === "completed";

  return (
    <div className="gi-tl-item" data-testid="event-timeline-item">
      <span className={cn("gi-tl-time", live && "now")}>{live ? "now" : fmtTime(event.starts_at)}</span>
      <span
        className={cn("gi-tl-node", live && "now", done && "dim")}
        style={{ "--tl-c": nodeColor(event) } as React.CSSProperties}
        aria-hidden
      />
      <article className={cn("gi-event-row", live && "live", done && "done")}>
        <div className="min-w-0">
          <p className="gi-event-title">{event.name}</p>
          <div className="gi-event-sub flex flex-wrap items-center gap-2">
            <SportPill sportKey={event.sport_key} />
            <span className="font-mono text-[11px]">
              {away?.display_name ?? "—"} @ {home?.display_name ?? "—"}
            </span>
          </div>
        </div>
        <span className="ml-auto flex flex-none items-center gap-3">
          <ParticipantScore home={home} away={away} />
          <StatusPill status={event.status} />
        </span>
      </article>
    </div>
  );
}

const NOTIFY_STORAGE_KEY = "sika.events.notify.v1";

interface NotifyPrefs {
  lineMoves: boolean;
  injuryNews: boolean;
  opsStatus: boolean;
}

const DEFAULT_NOTIFY: NotifyPrefs = { lineMoves: true, injuryNews: true, opsStatus: false };

function NotifyToggle({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div className={cn("gi-toggle-row", !checked && "off")}>
      <span>{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        className="gi-toggle"
        onClick={() => onChange(!checked)}
      />
    </div>
  );
}

/** Slate counts + notify preferences (spec 5e rail). */
function EventsRail({ events }: { events: EventRead[] }) {
  const live = events.filter((event) => isLive(event.status)).length;
  const upcoming = events.filter((event) => !isLive(event.status) && event.status !== "completed").length;
  const finals = events.filter((event) => event.status === "completed").length;

  const [prefs, setPrefs] = useState<NotifyPrefs>(DEFAULT_NOTIFY);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(NOTIFY_STORAGE_KEY);
      if (raw) setPrefs({ ...DEFAULT_NOTIFY, ...(JSON.parse(raw) as Partial<NotifyPrefs>) });
    } catch {
      /* corrupt / private mode — keep defaults */
    } finally {
      setHydrated(true);
    }
  }, []);

  function update(partial: Partial<NotifyPrefs>) {
    setPrefs((current) => {
      const next = { ...current, ...partial };
      try {
        window.localStorage.setItem(NOTIFY_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* quota — in-memory only */
      }
      return next;
    });
  }

  return (
    <div className="gi-rail" data-testid="events-rail">
      <span className="gi-micro-label rail">on the slate</span>
      <div className="gi-rail-stat">
        <span className="flex items-center gap-2">
          <span className="gi-glow-dot" style={{ "--gd": "var(--gi-green)" } as React.CSSProperties} aria-hidden />
          live
        </span>
        <span className="v" style={{ color: "var(--gi-green)" }}>{live}</span>
      </div>
      <div className="gi-rail-stat">
        <span className="flex items-center gap-2">
          <span className="gi-glow-dot" aria-hidden />
          upcoming
        </span>
        <span className="v">{upcoming}</span>
      </div>
      <div className="gi-rail-stat">
        <span className="flex items-center gap-2">
          <span className="gi-glow-dot" style={{ "--gd": "var(--color-cosmos-violet-500)" } as React.CSSProperties} aria-hidden />
          finals
        </span>
        <span className="v">{finals}</span>
      </div>
      <div className="gi-rail-divider" />
      <span className="gi-micro-label rail">notify me about</span>
      {hydrated && (
        <>
          <NotifyToggle
            label="line moves on my picks"
            checked={prefs.lineMoves}
            onChange={(next) => update({ lineMoves: next })}
          />
          <NotifyToggle
            label="injury news"
            checked={prefs.injuryNews}
            onChange={(next) => update({ injuryNews: next })}
          />
          <NotifyToggle
            label="ops & run status"
            checked={prefs.opsStatus}
            onChange={(next) => update({ opsStatus: next })}
          />
        </>
      )}
    </div>
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
      <EmptyState
        tone="error"
        title="Couldn&rsquo;t reach the events feed."
        description="The API didn&rsquo;t respond. Check that the backend is running, then try again."
      />
    );
  }

  const events = data ?? [];
  const filtered = mode === "dashboard"
    ? filterDashboardEvents(events)
    : day
      ? filterEventsForDay(events, day)
      : filterDashboardEvents(events);
  const ordered = timelineOrder(filtered);
  const emptyMessage = mode === "dashboard"
    ? "No live or upcoming events found."
    : "No events matched the selected date.";

  const timeline = (
    <div className="gi-timeline">
      {isLoading
        ? Array.from({ length: compact ? 4 : 6 }).map((_, index) => (
            <div key={index} className="gi-tl-item">
              <Skeleton className="h-16 w-full rounded-xl" />
            </div>
          ))
        : ordered.length === 0
          ? (
            <div className="gi-event-row" style={{ opacity: 0.8 }}>
              <p className="text-sm text-muted-foreground">{emptyMessage}</p>
            </div>
          )
          : ordered.map((event) => <TimelineItem key={event.id} event={event} />)}
    </div>
  );

  if (mode === "dashboard") {
    return timeline;
  }

  return (
    <div className="gi-cols">
      <div className="gi-cols-main">{timeline}</div>
      <div className="gi-cols-rail hidden xl:block">
        <EventsRail events={ordered} />
      </div>
    </div>
  );
}
