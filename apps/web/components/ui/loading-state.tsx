"use client";

import type { ReactElement } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

interface LoadingStateProps {
  /** Operator-readable label that announces what's loading. Required
   *  — screen readers should hear it. */
  label: string;
  /** Tone of the spinner + label. `accent` is the cosmos primary
   *  (blue); `muted` is a quieter variant for sections that should
   *  not pull focus. */
  tone?: "accent" | "muted";
  /** Spinner size. `md` is the default panel-scale loader. */
  size?: "sm" | "md" | "lg";
  /** Extend container className for layout (max-width, padding...). */
  className?: string;
}

const ICON_SIZE: Record<Required<LoadingStateProps>["size"], number> = {
  sm: 14,
  md: 18,
  lg: 22,
};

/**
 * Cosmos canonical full-section loader. Carries `role="status"` +
 * `aria-label` so screen readers announce what's loading when the
 * component mounts. Resolves DESIGN_SYSTEM.md §6.2 a11y gap on
 * ad-hoc spinners (the `<RefreshCw className="animate-spin">`
 * pattern carried no announcement).
 *
 * **When NOT to use this primitive:**
 * - For tabular loaders use `<Skeleton>` / `<SkeletonRow>` — they
 *   preserve layout, which prevents reflow when data arrives.
 * - For inline refresh-button spinners (the action is "refresh"
 *   not "loading state"), add `aria-label` to the button itself
 *   rather than wrapping a `<LoadingState>`.
 * - The bespoke `.sa-result-loading` pattern is intentional sika
 *   personality and stays bespoke on the stats assistant surface.
 */
export function LoadingState({
  label,
  tone = "accent",
  size = "md",
  className,
}: LoadingStateProps): ReactElement {
  return (
    <div
      role="status"
      aria-label={label}
      data-tone={tone}
      className={cn(
        "flex items-center justify-center gap-2 rounded-xl border border-border bg-surface px-4 py-8 text-sm",
        tone === "accent" && "text-accent",
        tone === "muted" && "text-muted-foreground",
        className,
      )}
    >
      <Loader2
        size={ICON_SIZE[size]}
        className="animate-spin"
        aria-hidden
      />
      <span>{label}</span>
    </div>
  );
}
