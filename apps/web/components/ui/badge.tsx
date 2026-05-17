import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  [
    "inline-flex items-center gap-1",
    "font-sans text-xs font-medium",
    "rounded px-1.5 py-0.5",
    "border",
    "transition-colors duration-[120ms]",
  ],
  {
    variants: {
      variant: {
        default: "bg-surface border-border text-muted-foreground",
        accent: "bg-accent/10 border-accent/25 text-accent",
        positive: "bg-positive/10 border-positive/25 text-positive",
        negative: "bg-negative/10 border-negative/25 text-negative",
        warning: "bg-warning/10 border-warning/25 text-warning",
        outline: "bg-transparent border-border-bright text-foreground",
        /* sport-specific */
        nba: "bg-sport-nba/10 border-sport-nba/25 text-sport-nba",
        nfl: "bg-sport-nfl/10 border-sport-nfl/25 text-sport-nfl",
        mlb: "bg-sport-mlb/10 border-sport-mlb/25 text-sport-mlb",
        wnba: "bg-sport-wnba/10 border-sport-wnba/25 text-sport-wnba",
        soccer: "bg-sport-soccer/10 border-sport-soccer/25 text-sport-soccer",
        tennis: "bg-sport-tennis/10 border-sport-tennis/25 text-sport-tennis",
        ufc: "bg-sport-ufc/10 border-sport-ufc/25 text-sport-ufc",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

type BadgeVariant = VariantProps<typeof badgeVariants>["variant"];

interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

/**
 * General-purpose label chip. Use for non-status labels: sport tags,
 * mode indicators, count badges, confidence-band hints, anything
 * that's a categorical / informational signal.
 *
 * **For settled-outcome semantics (won / lost / pending / cancelled)
 * use the `.outcome-pill` CSS class instead** — its color tokens are
 * tuned for outcome states (`--color-cosmos-outcome-*`) and it carries
 * the established sika status-chip shape.
 *
 * Disambiguation reference: DESIGN_SYSTEM.md §3 (Badge vs outcome-pill).
 */
export function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

/**
 * Sport-tinted badge. Picks the variant from the sport key
 * (`nba` → `bg-sport-nba/10` etc.) and renders the sport name
 * in uppercase. Use anywhere a sport label needs to read at a glance.
 */
export function SportBadge({ sport }: { sport: string }) {
  const key = sport.toLowerCase() as BadgeVariant;
  return (
    <Badge variant={key}>
      {sport.toUpperCase()}
    </Badge>
  );
}
