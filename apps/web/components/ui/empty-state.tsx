"use client";

import type { ReactElement, ReactNode } from "react";
import { cn } from "@/lib/utils";

type Tone = "default" | "error" | "warning" | "positive";

interface EmptyStateProps {
  /** Headline message. Keep terse — operators scan this in a glance. */
  title: string;
  /** Optional second-line context, typically the WHY. */
  description?: ReactNode;
  /** Tone modifies the indicator dot color + the container's border /
   *  background tint. Default = "informational" (no dot, plain
   *  border + surface). "error" / "warning" / "positive" each map to
   *  the matching cosmos semantic tone tokens with a glowing dot. */
  tone?: Tone;
  /** Optional decorative element rendered above the title. When
   *  provided, replaces the tone-driven indicator dot — use for orb
   *  patterns or custom icons on high-personality surfaces. */
  icon?: ReactNode;
  /** Optional action button(s). Renders below the description. */
  action?: ReactNode;
  /** Extend container className for layout (max-width, padding...). */
  className?: string;
}

/**
 * Cosmos canonical empty / error state primitive. Resolves
 * DESIGN_SYSTEM.md §5.4 ad-hoc empty-state proliferation + §6.1 a11y
 * gap (most empty states didn't announce themselves to screen
 * readers).
 *
 * Carries `role="status"` + `aria-live="polite"` so screen readers
 * announce the empty / error state when it appears after a load.
 *
 * **When NOT to use this primitive:**
 * - The `.trade-ticket-empty-orb` and `.sa-result-empty` orb patterns
 *   are sika-distinctive personality and stay bespoke — use them on
 *   high-personality surfaces (trade ticket, stats assistant).
 * - For tabular empty states use `.cosmos-table-empty` — its
 *   row-aligned layout fits inside a table-shaped container.
 */
export function EmptyState({
  title,
  description,
  tone = "default",
  icon,
  action,
  className,
}: EmptyStateProps): ReactElement {
  return (
    <div
      role="status"
      aria-live="polite"
      data-tone={tone}
      className={cn(
        "rounded-xl border px-4 py-8 text-center",
        tone === "default" && "border-border bg-surface",
        tone === "error" && "border-negative/30 bg-negative-dim",
        tone === "warning" && "border-warning/30 bg-warning/[0.08]",
        tone === "positive" && "border-positive/30 bg-positive-dim",
        className,
      )}
    >
      {icon ?? <ToneIndicator tone={tone} />}
      <p className="mt-3 text-sm font-medium text-foreground">{title}</p>
      {description != null && (
        <div className="mt-1 text-xs text-muted-foreground">{description}</div>
      )}
      {action != null && <div className="mt-4">{action}</div>}
    </div>
  );
}

interface ToneIndicatorProps {
  tone: Tone;
}

function ToneIndicator({ tone }: ToneIndicatorProps): ReactElement | null {
  if (tone === "default") return null;
  const dotClass = {
    error: "bg-negative shadow-[0_0_8px_0_var(--negative)]",
    warning: "bg-warning shadow-[0_0_8px_0_var(--warning)]",
    positive: "bg-positive shadow-[0_0_8px_0_var(--positive)]",
  }[tone];
  return (
    <div
      aria-hidden
      className="mx-auto flex h-2 w-2 items-center justify-center"
    >
      <span className={cn("h-2 w-2 rounded-full", dotClass)} />
    </div>
  );
}
