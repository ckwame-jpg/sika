import { cn } from "@/lib/utils";

/**
 * Smarter #24 — shared inline "T-Xm" indicator.
 *
 * - ``null`` minutes (market without a scheduled close) → renders nothing.
 * - ``minutes <= 0`` → "closing" badge (destructive color).
 * - ``minutes <= 30`` → red highlight, urgent.
 * - ``minutes < 60`` → "T-Xm" muted.
 * - ``minutes < 24*60`` → "T-XhYm" muted (suppresses the "0m" suffix).
 * - ``minutes >= 24*60`` → "T-Xd" muted; triage urgency does not apply.
 */
export function TimeToCloseBadge({ minutes }: { minutes: number | null }) {
  if (minutes === null) return null;
  if (minutes <= 0) {
    return (
      <span className="ml-2 inline-flex items-center text-2xs uppercase tracking-wide text-destructive">
        · closing
      </span>
    );
  }
  if (minutes >= 24 * 60) {
    const days = Math.floor(minutes / (24 * 60));
    return (
      <span className="ml-2 inline-flex items-center text-2xs uppercase tracking-wide text-muted-foreground">
        · T-{days}d
      </span>
    );
  }
  const urgent = minutes <= 30;
  const label =
    minutes >= 60
      ? `T-${Math.floor(minutes / 60)}h${minutes % 60 > 0 ? `${minutes % 60}m` : ""}`
      : `T-${minutes}m`;
  return (
    <span
      className={cn(
        "ml-2 inline-flex items-center text-2xs uppercase tracking-wide",
        urgent ? "text-destructive font-semibold" : "text-muted-foreground",
      )}
    >
      · {label}
    </span>
  );
}
