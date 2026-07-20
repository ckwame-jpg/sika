"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { ChevronDown, ChevronUp, X } from "lucide-react";
import { useParlayTray, MAX_TRAY_LEGS } from "./parlay-tray-store";
import { computePaperParlayQuote } from "./paper-parlay-quote";
import { KalshiComboDialog } from "./kalshi-combo-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchMyKalshiCredentials, keys, previewKalshiCombo } from "@/lib/api";
import { kalshiEnvLabel } from "@/lib/kalshi-env";
import type { KalshiComboPreviewRead } from "@/lib/types";
import type { TradeSelection } from "@/components/trade/trade-ticket";
import { cn } from "@/lib/utils";

/**
 * Debounced combo combinability check against Kalshi's collections.
 * Re-runs 500ms after the leg SET (tickers + sides) changes — price
 * drift alone doesn't re-query. Null result = check not applicable
 * (no creds / fewer than 2 legs / in flight).
 */
function useComboPreview(legs: TradeSelection[], enabled: boolean) {
  const [preview, setPreview] = useState<KalshiComboPreviewRead | null>(null);
  const [checking, setChecking] = useState(false);
  const requestRef = useRef(0);

  const legKey = legs
    .map((leg) => `${leg.ticker}:${leg.selectedSide.toLowerCase()}`)
    .join("|");

  useEffect(() => {
    if (!enabled || legs.length < 2) {
      setPreview(null);
      setChecking(false);
      return;
    }
    const requestId = ++requestRef.current;
    setChecking(true);
    const timer = setTimeout(() => {
      previewKalshiCombo({
        legs: legs.map((leg) => ({
          ticker: leg.ticker,
          side: leg.selectedSide.toLowerCase() as "yes" | "no",
          entry_price: leg.entryPrice ?? null,
          market_title: leg.displayLabel ?? leg.marketTitle ?? null,
          subject_name: leg.subjectName ?? null,
          stat_key: leg.statKey ?? null,
          threshold: leg.threshold ?? null,
        })),
      })
        .then((result) => {
          if (requestRef.current === requestId) setPreview(result);
        })
        .catch(() => {
          if (requestRef.current === requestId) {
            setPreview({
              combinable: false,
              reason: "couldn't check kalshi — try again",
              collection_ticker: null,
              existing_market_ticker: null,
              implied_price: null,
              quote_yes_bid: null,
              quote_yes_ask: null,
            });
          }
        })
        .finally(() => {
          if (requestRef.current === requestId) setChecking(false);
        });
    }, 500);
    return () => clearTimeout(timer);
    // legKey is the deliberate dependency — see docstring.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, legKey]);

  return { preview, checking };
}

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
  const { legs, stake, collapsed, removeLeg, setStake, setCollapsed, clear } = useParlayTray();
  const quote = useMemo(() => computePaperParlayQuote(legs), [legs]);

  // Local input string mirrors the store's parsed stake so the user
  // can type "$2" without the cursor jumping every keystroke. Sync
  // back to the store on each keystroke that parses cleanly.
  const [stakeInput, setStakeInput] = useState(stake != null ? String(stake) : "");

  // Real-combo affordance — only when Kalshi credentials are connected.
  const { data: kalshiCreds } = useSWR(keys.myKalshiCredentials, fetchMyKalshiCredentials);
  const kalshiConfigured = Boolean(kalshiCreds?.configured);
  const kalshiEnv = kalshiEnvLabel(kalshiCreds?.base_url);
  const { preview, checking } = useComboPreview(legs, kalshiConfigured);
  const [comboDialogOpen, setComboDialogOpen] = useState(false);

  if (legs.length === 0) return null;

  const canSave = legs.length >= 2 && Boolean(onSave);
  const canPlaceCombo = legs.length >= 2 && Boolean(preview?.combinable);
  const edgePositive = quote.edge > 0;

  // Live projection — uses the live tray combined price (a sweep over
  // the chips), so the operator sees their projected payout shift in
  // real time as they add or drop legs.
  const parsedStake = parseStake(stakeInput);
  const projection =
    parsedStake != null && Number.isFinite(quote.combinedMarketPrice) && quote.combinedMarketPrice > 0
      ? {
          payout: parsedStake / quote.combinedMarketPrice,
          profit: parsedStake / quote.combinedMarketPrice - parsedStake,
        }
      : null;

  return (
    <section
      className={cn("parlay-tray", collapsed && "parlay-tray-collapsed")}
      role="region"
      aria-label="Paper parlay tray"
      data-testid="parlay-tray"
      data-collapsed={collapsed ? "true" : "false"}
    >
      <div className="parlay-tray-inner">
        <header className="parlay-tray-header">
          <div className="parlay-tray-title">
            <button
              type="button"
              onClick={() => setCollapsed(!collapsed)}
              className="parlay-tray-toggle focus-visible:ring-focus"
              aria-label={collapsed ? "Expand parlay tray" : "Collapse parlay tray"}
              aria-expanded={!collapsed}
              data-testid="parlay-tray-toggle"
            >
              {collapsed ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            </button>
            <span className="parlay-tray-label">parlay</span>
            <span className="parlay-tray-count">
              {legs.length} of {MAX_TRAY_LEGS} legs
            </span>
            {/* Inline summary visible only while collapsed — keeps the
                key numbers (stake + projected payout + profit) at the
                operator's eye-line even with the body hidden. */}
            {collapsed && projection && (
              <span className="parlay-tray-collapsed-summary" data-testid="parlay-tray-collapsed-summary">
                <span className="parlay-tray-collapsed-stake">${parsedStake!.toFixed(2)}</span>
                <span className="parlay-tray-collapsed-arrow">→</span>
                <span className="parlay-tray-collapsed-payout">${projection.payout.toFixed(2)}</span>
                <span
                  className={cn(
                    "parlay-tray-collapsed-profit",
                    projection.profit >= 0 ? "text-positive" : "text-negative",
                  )}
                >
                  {projection.profit >= 0 ? "+" : ""}${projection.profit.toFixed(2)}
                </span>
              </span>
            )}
          </div>
          <div className="parlay-tray-header-actions">
            {collapsed && canSave && (
              <Button
                variant="primary"
                size="xs"
                onClick={onSave}
                data-testid="parlay-tray-save-collapsed"
              >
                Save
              </Button>
            )}
            <button
              type="button"
              onClick={clear}
              className="parlay-tray-clear focus-visible:ring-focus"
              data-testid="parlay-tray-clear"
            >
              clear
            </button>
          </div>
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

        <div className="parlay-tray-stake-row mt-3 grid gap-2 sm:grid-cols-[1fr_auto] sm:items-center">
          <div className="relative">
            <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-xs text-muted-foreground">
              $
            </span>
            <Input
              mono
              className="pl-6"
              inputMode="decimal"
              placeholder="Stake"
              value={stakeInput}
              onChange={(event) => {
                const value = event.target.value;
                setStakeInput(value);
                setStake(parseStake(value));
              }}
              data-testid="parlay-tray-stake"
              aria-label="Stake (USD, paper)"
            />
          </div>
          <div
            className="rounded-md border border-border/40 bg-surface-hover/30 px-3 py-1.5 sm:min-w-[180px]"
            data-testid="parlay-tray-projection"
          >
            <span className="block text-2xs uppercase tracking-wide text-muted-foreground/70">
              Projected payout
            </span>
            {projection ? (
              <span className="mt-0.5 flex items-baseline justify-between gap-2 font-mono text-xs">
                <span className="text-foreground">${projection.payout.toFixed(2)}</span>
                <span className={cn(projection.profit >= 0 ? "text-positive" : "text-negative")}>
                  {projection.profit >= 0 ? "+" : ""}${projection.profit.toFixed(2)}
                </span>
              </span>
            ) : (
              <span className="mt-0.5 block font-mono text-xs text-muted-foreground/60">—</span>
            )}
          </div>
        </div>

        {/* Kalshi combinability — mirrors what the mobile app's combo
            builder would tell you, BEFORE any order intent. Status-pill
            construction with a glow dot, matching the app's chip
            language (green = go, amber = reason, cyan = in motion). */}
        {kalshiConfigured && legs.length >= 2 && (
          <div className="mt-2" data-testid="parlay-tray-combinability">
            {checking ? (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-accent/30 bg-accent/5 px-2.5 py-1 text-2xs text-muted-foreground">
                <span
                  className="h-[5px] w-[5px] animate-pulse rounded-full bg-accent/80 shadow-[0_0_6px_rgba(140,220,255,0.6)]"
                  aria-hidden
                />
                checking kalshi combinability…
              </span>
            ) : preview?.combinable ? (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-positive/35 bg-positive/10 px-2.5 py-1 text-2xs text-positive">
                <span
                  className="h-[5px] w-[5px] rounded-full bg-positive shadow-[0_0_6px_rgba(33,202,145,0.7)]"
                  aria-hidden
                />
                combinable on kalshi ✓
                {preview.existing_market_ticker
                  ? " · live combo market exists"
                  : " · will mint the combo market"}
                {preview.quote_yes_ask != null &&
                  ` · ask ${(preview.quote_yes_ask * 100).toFixed(0)}¢`}
              </span>
            ) : preview ? (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-warning/35 bg-warning/5 px-2.5 py-1 text-2xs text-warning">
                <span className="h-[5px] w-[5px] rounded-full bg-warning" aria-hidden />
                {preview.reason}
              </span>
            ) : null}
          </div>
        )}

        {/* One button system, two tints (same pairing as the trade
            ticket): violet gi-btn = paper, amber gi-btn-live = real
            money. Identical geometry — only the tint says "careful". */}
        <div className="parlay-tray-actions">
          <button
            type="button"
            className="gi-btn"
            disabled={!canSave}
            onClick={onSave}
            data-testid="parlay-tray-save"
          >
            {legs.length < 2 ? "Add another leg" : "Save paper parlay"}
          </button>
          {kalshiConfigured && (
            <button
              type="button"
              className="gi-btn-live"
              disabled={!canPlaceCombo}
              onClick={() => setComboDialogOpen(true)}
              data-testid="parlay-tray-place-kalshi"
            >
              place combo on kalshi{kalshiEnv === "demo" ? " · demo" : ""}
            </button>
          )}
        </div>
      </div>

      {kalshiConfigured && (
        <KalshiComboDialog
          open={comboDialogOpen}
          onOpenChange={setComboDialogOpen}
          environment={kalshiEnv}
          preview={preview}
        />
      )}
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

function parseStake(raw: string): number | null {
  if (!raw.trim()) return null;
  const value = Number(raw.replace(/[$,\s]/g, ""));
  if (!Number.isFinite(value) || value <= 0) return null;
  return value;
}
