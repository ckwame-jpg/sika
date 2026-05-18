"use client";

import { useMemo } from "react";
import { X } from "lucide-react";
import { useParlayTray, MAX_TRAY_LEGS } from "./parlay-tray-store";
import { computePaperParlayQuote } from "./paper-parlay-quote";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * PAPER_PARLAY_SCOPE.md step 5 — docked tray for the operator-built
 * paper parlay. Renders at the bottom of the trade-desk page when at
 * least one leg has been added; collapses to nothing when empty.
 *
 * Step 5 ships the tray + live joint math + chip removal + clear. The
 * Save button is wired in step 6 (it opens the dialog that POSTs to
 * /paper-parlays). For now ``onSave`` is an optional callback so the
 * parent can stub it; the button is disabled when fewer than 2 legs
 * are in the tray (matching the backend's MIN_LEG_COUNT).
 */

interface ParlayTrayProps {
  /** Step 6 will pass an onSave that opens the dialog. Step 5 ships
   *  with no caller wiring; the button stays disabled (and labeled
   *  "Save paper parlay") until the dialog lands. */
  onSave?: () => void;
}

export function ParlayTray({ onSave }: ParlayTrayProps) {
  const { legs, removeLeg, clear } = useParlayTray();
  const quote = useMemo(() => computePaperParlayQuote(legs), [legs]);

  if (legs.length === 0) return null;

  const canSave = legs.length >= 2 && Boolean(onSave);
  const edgePositive = quote.edge > 0;

  return (
    <section
      className="parlay-tray"
      role="region"
      aria-label="Paper parlay tray"
      data-testid="parlay-tray"
    >
      <div className="parlay-tray-inner">
        <header className="parlay-tray-header">
          <div className="parlay-tray-title">
            <span className="parlay-tray-label">parlay</span>
            <span className="parlay-tray-count">
              {legs.length} of {MAX_TRAY_LEGS} legs
            </span>
          </div>
          <button
            type="button"
            onClick={clear}
            className="parlay-tray-clear focus-visible:ring-focus"
            data-testid="parlay-tray-clear"
          >
            clear
          </button>
        </header>

        <ol className="parlay-tray-chips" data-testid="parlay-tray-chips">
          {legs.map((leg) => (
            <li key={leg.ticker} className="parlay-tray-chip">
              <span className="parlay-tray-chip-label">
                {chipLabel(leg)}
              </span>
              <span className="parlay-tray-chip-side">
                {leg.selectedSide.toUpperCase()} @{" "}
                {formatPrice(leg.entryPrice)}
              </span>
              <button
                type="button"
                onClick={() => removeLeg(leg.ticker)}
                className="parlay-tray-chip-remove focus-visible:ring-focus"
                data-testid={`parlay-tray-chip-remove-${leg.ticker}`}
                aria-label={`Remove ${chipLabel(leg)} from parlay`}
              >
                <X size={11} />
              </button>
            </li>
          ))}
        </ol>

        <div className="parlay-tray-quote" data-testid="parlay-tray-quote">
          <QuoteStat label="combined" value={formatPrice(quote.combinedMarketPrice)} />
          <QuoteStat label="odds" value={quote.americanOdds} />
          <QuoteStat label="joint prob" value={formatPercent(quote.combinedModelProbability)} />
          <QuoteStat
            label="edge"
            value={formatEdge(quote.edge)}
            tone={edgePositive ? "positive" : "negative"}
          />
        </div>

        <div className="parlay-tray-actions">
          <Button
            variant="primary"
            size="sm"
            disabled={!canSave}
            onClick={onSave}
            data-testid="parlay-tray-save"
          >
            {legs.length < 2 ? "Add another leg" : "Save paper parlay"}
          </Button>
        </div>
      </div>
    </section>
  );
}

interface QuoteStatProps {
  label: string;
  value: string;
  tone?: "positive" | "negative";
}

function QuoteStat({ label, value, tone }: QuoteStatProps) {
  return (
    <div className="parlay-tray-quote-stat">
      <span className="parlay-tray-quote-label">{label}</span>
      <span
        className={cn(
          "parlay-tray-quote-value",
          tone === "positive" && "text-positive",
          tone === "negative" && "text-negative",
        )}
      >
        {value}
      </span>
    </div>
  );
}

/** Compact chip label: prefer "Player NN+ stat" for player props,
 *  fall back to displayLabel / marketTitle for game lines. */
function chipLabel(leg: {
  subjectName?: string | null;
  threshold?: number | null;
  statKey?: string | null;
  displayLabel?: string | null;
  marketTitle?: string | null;
}): string {
  if (leg.subjectName && leg.statKey && leg.threshold != null) {
    return `${leg.subjectName} ${leg.threshold}+ ${leg.statKey.replace(/_/g, " ")}`;
  }
  return leg.displayLabel || leg.marketTitle || "leg";
}

function formatPrice(price: number | null | undefined): string {
  if (price == null || !Number.isFinite(price)) return "—";
  // Always 2 decimals so chips line up; expressed as cents-style "0.65".
  return price.toFixed(2);
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function formatEdge(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const pct = value * 100;
  const prefix = pct >= 0 ? "+" : "";
  return `${prefix}${pct.toFixed(1)}%`;
}
