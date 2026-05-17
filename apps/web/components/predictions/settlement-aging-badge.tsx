"use client";

import type { ReactElement } from "react";
import { Clock } from "lucide-react";
import type { SettlementAgingRead } from "@/lib/types";
import { cn } from "@/lib/utils";

interface SettlementAgingBadgeProps {
  aging: SettlementAgingRead;
}

interface BucketCell {
  label: string;
  value: number;
  toneClass: string;
  testId: string;
}

/** Smarter #26 phase 2 — operator-facing badge for the settlement
 *  aging buckets backend computed in
 *  ``apps/api/app/services/predictions.compute_settlement_aging``.
 *
 *  Renders a horizontal pill row with one cell per bucket
 *  (0-1h / 1-6h / 6-24h / 24h+) plus a total-past-close roll-up. The
 *  longer the bucket the redder the tone — the operator should
 *  notice the right-edge cell first when something is genuinely
 *  stuck. When every bucket is zero we render the "all clear"
 *  state so operators see explicit confirmation that nothing is
 *  past close (rather than wondering if the badge is broken or the
 *  data is stale).
 */
export function SettlementAgingBadge({ aging }: SettlementAgingBadgeProps): ReactElement {
  const cells: BucketCell[] = [
    {
      label: "0–1h",
      value: aging.bucket_0_to_1h,
      toneClass: aging.bucket_0_to_1h > 0 ? "pending" : "",
      testId: "settlement-aging-bucket-0-1h",
    },
    {
      label: "1–6h",
      value: aging.bucket_1_to_6h,
      toneClass: aging.bucket_1_to_6h > 0 ? "pending" : "",
      testId: "settlement-aging-bucket-1-6h",
    },
    {
      label: "6–24h",
      value: aging.bucket_6_to_24h,
      // 6-24h is past the operator's "should be settled by next refresh"
      // expectation. Mark with the higher-attention tone.
      toneClass: aging.bucket_6_to_24h > 0 ? "lost" : "",
      testId: "settlement-aging-bucket-6-24h",
    },
    {
      label: "24h+",
      value: aging.bucket_beyond_24h,
      // Anything still pending past 24h is broken plumbing — the
      // settlement worker should have caught it. Same tone as 6-24h
      // but the position-rightmost emphasis already draws the eye.
      toneClass: aging.bucket_beyond_24h > 0 ? "lost" : "",
      testId: "settlement-aging-bucket-beyond-24h",
    },
  ];

  if (aging.total_pending_past_close === 0) {
    return (
      <div className="stats-tile" data-testid="settlement-aging-badge">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Clock size={14} className="text-muted-foreground" />
            <p className="stats-tile-label">Settlement Aging</p>
          </div>
          <span className="outcome-pill settled">all clear</span>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          No predictions stuck past their market close.
        </p>
      </div>
    );
  }

  return (
    <div className="stats-tile" data-testid="settlement-aging-badge">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Clock size={14} className="text-muted-foreground" />
          <p className="stats-tile-label">Settlement Aging</p>
        </div>
        <span
          className={cn(
            "outcome-pill",
            // The roll-up tone follows the worst bucket: any 6h+ rows
            // → "lost" (red); only 0-6h rows → "pending" (amber).
            aging.bucket_6_to_24h > 0 || aging.bucket_beyond_24h > 0
              ? "lost"
              : "pending",
          )}
          data-testid="settlement-aging-total"
        >
          {aging.total_pending_past_close} past close
        </span>
      </div>
      <div className="mt-2 grid grid-cols-4 gap-2">
        {cells.map((cell) => (
          <div
            key={cell.label}
            className={cn(
              "rounded-md border border-border/60 px-2 py-1.5 text-center",
              cell.value === 0 && "opacity-50",
            )}
            data-testid={cell.testId}
          >
            <p className="text-[11px] text-muted-foreground">{cell.label}</p>
            <p className="text-base font-mono">
              {cell.value}
              {cell.toneClass ? (
                <span
                  className={cn("outcome-pill ml-1 px-1.5 py-0.5 text-2xs", cell.toneClass)}
                  aria-hidden
                >
                  •
                </span>
              ) : null}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}
