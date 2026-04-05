import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import { compareAsc, format, formatDistanceToNowStrict, isSameDay, parseISO } from "date-fns";
import type { EventRead } from "./types";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function fmtPrice(price: number | null | undefined): string {
  if (price == null) return "—";
  return `${Math.round(price * 100)}¢`;
}

export function fmtPercent(value: number | null | undefined, decimals = 1): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(decimals)}%`;
}

export function fmtEdge(edge: number): string {
  const sign = edge >= 0 ? "+" : "";
  return `${sign}${(edge * 100).toFixed(1)}%`;
}

export function fmtVolume(vol: number | null | undefined): string {
  if (vol == null) return "—";
  if (vol >= 1_000_000) return `${(vol / 1_000_000).toFixed(1)}M`;
  if (vol >= 1_000) return `${(vol / 1_000).toFixed(1)}K`;
  return String(Math.round(vol));
}

export function fmtContractPnl(value: number | null | undefined): string {
  if (value == null) return "—";
  const cents = value * 100;
  const sign = cents >= 0 ? "+" : "";
  return `${sign}${cents.toFixed(1)}¢`;
}

export function fmtDatetime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return format(parseISO(iso), "MMM d, h:mm a");
  } catch {
    return iso;
  }
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return format(parseISO(iso), "MMM d, yyyy");
  } catch {
    return iso;
  }
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return format(parseISO(iso), "h:mm a");
  } catch {
    return iso;
  }
}

export function fmtStartsAt(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "—";
  try {
    const parsed = parseISO(iso);
    return format(parsed, isSameDay(parsed, now) ? "h:mm a" : "MMM d, h:mm a");
  } catch {
    return iso;
  }
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return formatDistanceToNowStrict(parseISO(iso), { addSuffix: true });
  } catch {
    return iso;
  }
}

export const SPORT_OPTIONS = [
  { value: "NBA", label: "NBA", colorClass: "text-sport-nba" },
  { value: "NFL", label: "NFL", colorClass: "text-sport-nfl" },
  { value: "MLB", label: "MLB", colorClass: "text-sport-mlb" },
  { value: "SOCCER", label: "Soccer", colorClass: "text-sport-soccer" },
  { value: "TENNIS", label: "Tennis", colorClass: "text-sport-tennis" },
] as const;

export const SPORT_COLOR: Record<string, string> = {
  NBA: "sport-nba",
  NFL: "sport-nfl",
  MLB: "sport-mlb",
  SOCCER: "sport-soccer",
  TENNIS: "sport-tennis",
};

export function sportColorClass(sportKey: string | null | undefined): string {
  if (!sportKey) return "text-muted-foreground";
  return `text-${SPORT_COLOR[sportKey.toUpperCase()] ?? "muted-foreground"}`;
}

export function sportLabel(sportKey: string | null | undefined): string {
  if (!sportKey) return "All sports";
  return SPORT_OPTIONS.find((option) => option.value === sportKey)?.label ?? sportKey;
}

export function eventStatusLabel(status: string): string {
  const map: Record<string, string> = {
    scheduled: "Scheduled",
    in_progress: "Live",
    completed: "Final",
    postponed: "Postponed",
    cancelled: "Cancelled",
  };
  return map[status] ?? status;
}

export function isLive(status: string): boolean {
  return status === "in_progress";
}

export function isFinishedEventStatus(status: string): boolean {
  return status === "completed" || status === "cancelled";
}

export function getLocalDateInputValue(date = new Date()): string {
  return format(date, "yyyy-MM-dd");
}

function parseEventDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  try {
    const value = parseISO(iso);
    return Number.isNaN(value.getTime()) ? null : value;
  } catch {
    return null;
  }
}

function compareEventStartsAsc(left: EventRead, right: EventRead): number {
  const leftDate = parseEventDate(left.starts_at);
  const rightDate = parseEventDate(right.starts_at);
  if (leftDate && rightDate) return compareAsc(leftDate, rightDate);
  if (leftDate) return -1;
  if (rightDate) return 1;
  return 0;
}

export function filterDashboardEvents(events: EventRead[], now = new Date()): EventRead[] {
  return [...events]
    .filter((event) => {
      if (isLive(event.status)) return true;
      if (isFinishedEventStatus(event.status)) return false;
      const startsAt = parseEventDate(event.starts_at);
      return startsAt != null && startsAt >= now;
    })
    .sort((left, right) => {
      const leftLive = isLive(left.status);
      const rightLive = isLive(right.status);
      if (leftLive != rightLive) return leftLive ? -1 : 1;
      return compareEventStartsAsc(left, right);
    });
}

export function filterEventsForDay(
  events: EventRead[],
  day: string,
  now = new Date(),
): EventRead[] {
  const today = getLocalDateInputValue(now);
  const includeFinished = day < today;

  return [...events]
    .filter((event) => {
      const startsAt = parseEventDate(event.starts_at);
      if (!startsAt) return false;
      if (format(startsAt, "yyyy-MM-dd") !== day) return false;

      if (includeFinished) return true;
      if (isLive(event.status)) return true;
      if (isFinishedEventStatus(event.status)) return false;
      return startsAt >= now;
    })
    .sort((left, right) => {
      if (includeFinished) return compareEventStartsAsc(left, right);
      const leftLive = isLive(left.status);
      const rightLive = isLive(right.status);
      if (leftLive != rightLive) return leftLive ? -1 : 1;
      return compareEventStartsAsc(left, right);
    });
}

export function pnlClass(pnl: number | null): string {
  if (pnl == null) return "text-muted-foreground";
  return pnl >= 0 ? "text-positive" : "text-negative";
}

export function sideClass(side: string): string {
  return side.toLowerCase() === "yes" ? "text-positive" : "text-negative";
}

export function edgeClass(edge: number): string {
  if (edge >= 0.08) return "text-positive";
  if (edge >= 0.04) return "text-warning";
  return "text-muted-foreground";
}

export function confidenceWidth(confidence: number): string {
  return `${Math.round(confidence * 100)}%`;
}
