// Bug #30 — shared sport-tint map. Previously duplicated byte-for-byte
// in ``apps/web/components/trade/trade-desk.tsx`` and
// ``apps/web/components/events/events-feed.tsx``. The two callers also
// had slightly different fallback colors when the sport key wasn't in
// the map; ``sportTint`` accepts an optional ``fallback`` so each
// caller can preserve its prior visual choice without re-introducing
// the constant.

export const SPORT_TINTS: Record<string, string> = {
  nba: "var(--sport-nba)",
  nfl: "var(--sport-nfl)",
  mlb: "var(--sport-mlb)",
  soccer: "var(--sport-soccer)",
  tennis: "var(--sport-tennis)",
  ufc: "var(--sport-ufc)",
};

export const DEFAULT_SPORT_TINT = "var(--color-cosmos-violet-default-tint)";

export function sportTint(sportKey: string | null | undefined, fallback: string = DEFAULT_SPORT_TINT): string {
  if (!sportKey) return fallback;
  return SPORT_TINTS[sportKey.toLowerCase()] ?? fallback;
}
