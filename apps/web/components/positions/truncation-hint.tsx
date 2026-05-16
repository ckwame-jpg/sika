// Bug #28 follow-up — small banner shown above the positions /
// orders tables when the API response was capped at the limit.
// Surfaces what the paginated ``/positions`` endpoint exposes so
// operators don't silently lose visibility into older rows.

interface TruncationHintProps {
  /** Number of rows actually rendered (already capped by the API). */
  visibleCount: number;
  /** Query-param name the operator can raise to see more. */
  limitParam: "paper_limit" | "demo_limit";
}

export function TruncationHint({ visibleCount, limitParam }: TruncationHintProps) {
  return (
    <div
      className="mb-2 rounded-md border border-amber-700/40 bg-amber-950/20 px-3 py-2 text-xs text-amber-200"
      role="note"
    >
      Showing {visibleCount} most recent. Older rows exist beyond the cap;
      raise <code>{limitParam}</code> on <code>/positions</code> (max 500)
      or page later via cursor.
    </div>
  );
}
