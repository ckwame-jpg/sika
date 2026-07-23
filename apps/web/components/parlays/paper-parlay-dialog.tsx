"use client";

import { useEffect, useMemo, useState } from "react";
import { mutate } from "swr";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { keys, openPaperParlay, quotePaperParlay } from "@/lib/api";
import type { PaperParlayQuoteRead } from "@/lib/types";
import type { TradeSelection } from "@/components/trade/trade-ticket";
import { useParlayTray } from "./parlay-tray-store";
import { computePaperParlayQuote } from "./paper-parlay-quote";
import { usePaperParlayServerQuote } from "./use-paper-parlay-quote";

interface PaperParlayDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * PAPER_PARLAY_SCOPE.md step 6 — save dialog for the operator's
 * parlay tray. Opens when the tray's "Save paper parlay" button is
 * clicked.
 *
 * Reads the tray contents via ``useParlayTray`` (single source of
 * truth — no leg-list prop drilling). On submit:
 *
 *   1. POST /paper-parlays with the tray's snapshot prices (decision
 *      #3: the operator's saved entry prices, not current market).
 *   2. On success: SWR-mutate the /positions key so the new parlay
 *      appears in the portfolio table on the next render.
 *   3. Clear the tray (it's been saved; the operator wants a fresh
 *      tray for the next pick).
 *   4. Close the dialog.
 *
 * On failure: surface the error message inline (the backend's
 * validation strings — "Market not found", "is not open", etc. —
 * are operator-readable as-is and don't need translation).
 */
export function PaperParlayDialog({ open, onOpenChange }: PaperParlayDialogProps) {
  const { legs, stake: trayStake, setStake, clear } = useParlayTray();
  const {
    data: serverQuote,
    error: serverQuoteError,
    isLoading: serverQuoteLoading,
    quoteRequest,
    quoteKey,
  } = usePaperParlayServerQuote(legs);
  const [stakeInput, setStakeInput] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmationQuote, setConfirmationQuote] =
    useState<PaperParlayQuoteRead | null>(null);
  const displayedQuote = confirmationQuote ?? serverQuote;
  const priceQuote = useMemo(
    () => computePaperParlayQuote(legs, displayedQuote?.combined_market_price),
    [legs, displayedQuote?.combined_market_price],
  );

  // Inherit the stake the operator already typed in the tray. They
  // shouldn't have to retype it just to hit "Save". Reset every open
  // so a previous save's notes don't bleed into the next attempt
  // (codex pattern 5).
  useEffect(() => {
    if (!open) return;
    setStakeInput(trayStake != null ? String(trayStake) : "");
    setNotes("");
    setError(null);
    setSubmitting(false);
    setConfirmationQuote(null);
  }, [open, trayStake]);

  useEffect(() => {
    setConfirmationQuote(null);
  }, [quoteKey]);

  const parsedStake = parseStake(stakeInput);
  const canSubmit =
    !submitting &&
    legs.length >= 2 &&
    parsedStake !== null &&
    parsedStake > 0;
  const potentialPayout =
    parsedStake !== null
      ? priceQuote.potentialPayoutForStake(parsedStake)
      : null;

  async function handleSubmit() {
    if (!canSubmit || parsedStake === null) return;
    if (quoteRequest === null) {
      setError("The current legs cannot be quoted. Review their sides and prices.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      // Saving always performs a fresh server resolution. If the canonical
      // values moved since the operator last saw them, update the dialog and
      // require one more click rather than silently saving a changed quote.
      const freshQuote = await quotePaperParlay(quoteRequest);
      if (quoteKey) await mutate(quoteKey, freshQuote, false);
      if (
        displayedQuote == null ||
        !sameConfirmableQuote(displayedQuote, freshQuote)
      ) {
        setConfirmationQuote(freshQuote);
        setError(
          "Quote updated. Review the refreshed price, probability, and edge, then save again.",
        );
        return;
      }
      const payload = {
        stake: parsedStake,
        notes: notes.trim() || null,
        legs: quoteRequest.legs,
        expected_quote: {
          combined_market_price: freshQuote.combined_market_price,
          joint_probability: freshQuote.joint_probability,
          edge: freshQuote.edge,
        },
      };
      await openPaperParlay(payload);
      // /positions is the portfolio aggregator; mutating its key
      // makes the new parlay appear without a manual reload.
      await mutate(keys.positions);
      clear();
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save parlay.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Save paper parlay</DialogTitle>
          <DialogDescription>
            Locks the current tray prices as a paper-only wager. Settles when every leg&apos;s underlying prediction resolves.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          <ol className="paper-parlay-dialog-legs" data-testid="paper-parlay-dialog-legs">
            {legs.map((leg) => (
              <li key={leg.ticker} className="paper-parlay-dialog-leg">
                <span className="paper-parlay-dialog-leg-label">{legSummary(leg)}</span>
                <span className="paper-parlay-dialog-leg-side">
                  {leg.selectedSide.toUpperCase()} @ {formatPrice(leg.entryPrice)}
                </span>
              </li>
            ))}
          </ol>

          <div className="paper-parlay-dialog-quote">
            <QuoteRow label="combined price" value={formatPrice(priceQuote.combinedMarketPrice)} />
            <QuoteRow label="american odds" value={priceQuote.americanOdds} />
            <QuoteRow
              label="model joint"
              value={formatServerQuoteValue(
                displayedQuote?.joint_probability,
                serverQuoteLoading && displayedQuote == null,
                Boolean(serverQuoteError),
                formatPercent,
              )}
            />
            <QuoteRow
              label="edge"
              value={formatServerQuoteValue(
                displayedQuote?.edge,
                serverQuoteLoading && displayedQuote == null,
                Boolean(serverQuoteError),
                formatEdge,
              )}
            />
          </div>

          <label className="paper-parlay-dialog-field" htmlFor="paper-parlay-stake">
            <span className="paper-parlay-dialog-field-label">Stake (USD, paper)</span>
            <Input
              id="paper-parlay-stake"
              data-testid="paper-parlay-dialog-stake"
              type="text"
              inputMode="decimal"
              value={stakeInput}
              onChange={(e) => {
                const next = e.target.value;
                setStakeInput(next);
                // Mirror back to the tray so the operator's working
                // stake survives a cancel + reopen.
                setStake(parseStake(next));
              }}
              placeholder="100"
              autoFocus
            />
            {potentialPayout !== null && (
              <span
                className="paper-parlay-dialog-payout"
                data-testid="paper-parlay-dialog-payout"
              >
                potential payout on win: ${potentialPayout.toFixed(2)}
              </span>
            )}
          </label>

          <label className="paper-parlay-dialog-field" htmlFor="paper-parlay-notes">
            <span className="paper-parlay-dialog-field-label">Notes (optional)</span>
            <Input
              id="paper-parlay-notes"
              data-testid="paper-parlay-dialog-notes"
              type="text"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="e.g. correlated SGP, betting on early-game tempo"
            />
          </label>

          {error && (
            <p
              role="alert"
              className="paper-parlay-dialog-error"
              data-testid="paper-parlay-dialog-error"
            >
              {error}
            </p>
          )}
        </DialogBody>

        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={handleSubmit}
            disabled={!canSubmit}
            data-testid="paper-parlay-dialog-submit"
          >
            {submitting ? "Saving…" : "Save paper parlay"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function QuoteRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="paper-parlay-dialog-quote-row">
      <span className="paper-parlay-dialog-quote-label">{label}</span>
      <span className="paper-parlay-dialog-quote-value">{value}</span>
    </div>
  );
}

function legSummary(leg: TradeSelection): string {
  if (leg.subjectName && leg.statKey && leg.threshold != null) {
    return `${leg.subjectName} ${leg.threshold}+ ${leg.statKey.replace(/_/g, " ")}`;
  }
  return leg.displayLabel || leg.marketTitle || leg.ticker;
}

function parseStake(raw: string): number | null {
  if (!raw.trim()) return null;
  const value = Number(raw.replace(/[$,\s]/g, ""));
  if (!Number.isFinite(value) || value <= 0) return null;
  return value;
}

function formatPrice(price: number | null | undefined): string {
  if (price == null || !Number.isFinite(price)) return "—";
  if (price > 0 && price < 0.01) return price.toFixed(6);
  return price.toFixed(2);
}

function sameConfirmableQuote(
  left: PaperParlayQuoteRead,
  right: PaperParlayQuoteRead,
): boolean {
  return (
    left.combined_market_price === right.combined_market_price &&
    left.joint_probability === right.joint_probability &&
    left.edge === right.edge
  );
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

function formatServerQuoteValue(
  value: number | undefined,
  loading: boolean,
  failed: boolean,
  formatter: (value: number) => string,
): string {
  if (value != null) return formatter(value);
  if (loading) return "loading…";
  if (failed) return "unavailable";
  return "—";
}
