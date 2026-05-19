"use client";

import { useEffect, useMemo, useState } from "react";
import { Pencil, Check, X } from "lucide-react";
import useSWR from "swr";
import { fetchPositions, keys } from "@/lib/api";
import type {
  PaperParlayRead,
  PaperPositionRead,
  PositionsRead,
} from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const LOCAL_STORAGE_KEY = "sika.paper.startingBankroll.v1";
const DEFAULT_STARTING_BANKROLL = 1000;

/**
 * Paper-trade earnings tile. Mirrors the Kalshi account-picks card
 * layout: four key stats in a responsive grid. The starting bankroll
 * is a localStorage-backed setting (per-browser) that the operator
 * can adjust inline — there's no auto-deducting cash account, just a
 * notional reference point for the earnings %.
 *
 * Reads paper data from the shared ``/positions`` SWR key. No new
 * backend surface needed.
 */
export function PaperEarningsCard() {
  const { data, isLoading } = useSWR<PositionsRead>(keys.positions, fetchPositions);

  const [startingBankroll, setStartingBankroll] = useState<number>(DEFAULT_STARTING_BANKROLL);
  const [hydrated, setHydrated] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draftInput, setDraftInput] = useState("");

  // Hydrate the starting bankroll from localStorage on mount. Guarded
  // by ``hydrated`` so the first render doesn't flash the default
  // before showing the stored value.
  useEffect(() => {
    if (typeof window === "undefined") {
      setHydrated(true);
      return;
    }
    try {
      const raw = window.localStorage.getItem(LOCAL_STORAGE_KEY);
      const parsed = raw == null ? null : Number(raw);
      if (parsed != null && Number.isFinite(parsed) && parsed > 0) {
        setStartingBankroll(parsed);
      }
    } catch {
      // Private mode / quota / corrupt — fall back to default.
    } finally {
      setHydrated(true);
    }
  }, []);

  function commitDraft() {
    const value = Number(draftInput.replace(/[$,\s]/g, ""));
    if (Number.isFinite(value) && value > 0) {
      setStartingBankroll(value);
      try {
        window.localStorage.setItem(LOCAL_STORAGE_KEY, String(value));
      } catch {
        // Quota — keep in-memory value; setting won't survive reload.
      }
    }
    setEditing(false);
  }

  // Include the legacy bucket in totals — those bets are still
  // history the operator made, and excluding them gave a misleading
  // "+$0.00 realized PnL" even after a -$50 legacy parlay had
  // settled. Per-user and legacy share the same PaperPosition /
  // PaperParlay schema, so they can be concatenated cleanly here.
  const positions = useMemo(
    () => [...(data?.paper_positions ?? []), ...(data?.legacy_paper_positions ?? [])],
    [data?.paper_positions, data?.legacy_paper_positions],
  );
  const parlays = useMemo(
    () => [...(data?.paper_parlays ?? []), ...(data?.legacy_paper_parlays ?? [])],
    [data?.paper_parlays, data?.legacy_paper_parlays],
  );

  const totals = useMemo(() => computeTotals(positions, parlays), [positions, parlays]);
  const netPosition = startingBankroll + totals.realizedPnl - totals.openExposure;
  const earningsPct =
    startingBankroll > 0 ? (totals.realizedPnl / startingBankroll) * 100 : 0;

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" data-testid="paper-earnings-grid">
        <BankrollMetric
          startingBankroll={startingBankroll}
          hydrated={hydrated}
          editing={editing}
          draftInput={draftInput}
          onBeginEdit={() => {
            setDraftInput(String(startingBankroll));
            setEditing(true);
          }}
          onDraftChange={setDraftInput}
          onCommit={commitDraft}
          onCancel={() => setEditing(false)}
        />
        <Metric
          label="Open Exposure"
          loading={isLoading}
          value={fmtDollars(totals.openExposure)}
          note={`${totals.openCount} open · ${totals.pendingParlays} parlay${totals.pendingParlays === 1 ? "" : "s"}`}
          testId="paper-earnings-open"
        />
        <Metric
          label="Realized PnL"
          loading={isLoading}
          value={fmtSignedDollars(totals.realizedPnl)}
          tone={totals.realizedPnl >= 0 ? "positive" : "negative"}
          note={
            totals.closedCount + totals.settledParlays > 0
              ? `${totals.closedCount + totals.settledParlays} settled`
              : "no settled bets yet"
          }
          testId="paper-earnings-realized"
        />
        <Metric
          label="Net Position"
          loading={isLoading}
          value={fmtDollars(netPosition)}
          tone={netPosition >= startingBankroll ? "positive" : "negative"}
          note={`${earningsPct >= 0 ? "+" : ""}${earningsPct.toFixed(1)}% vs start`}
          testId="paper-earnings-net"
        />
      </div>
    </div>
  );
}

interface BankrollMetricProps {
  startingBankroll: number;
  hydrated: boolean;
  editing: boolean;
  draftInput: string;
  onBeginEdit: () => void;
  onDraftChange: (next: string) => void;
  onCommit: () => void;
  onCancel: () => void;
}

function BankrollMetric({
  startingBankroll,
  hydrated,
  editing,
  draftInput,
  onBeginEdit,
  onDraftChange,
  onCommit,
  onCancel,
}: BankrollMetricProps) {
  return (
    <div className="rounded border border-border px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[11px] uppercase text-muted-foreground">Starting Bankroll</p>
        {!editing && hydrated && (
          <button
            type="button"
            onClick={onBeginEdit}
            aria-label="Edit starting bankroll"
            className="text-muted-foreground/70 hover:text-foreground focus-visible:ring-focus"
            data-testid="paper-earnings-bankroll-edit"
          >
            <Pencil size={11} />
          </button>
        )}
      </div>
      {!hydrated ? (
        <Skeleton className="mt-1 h-5 w-20" />
      ) : editing ? (
        <div className="mt-1 flex items-center gap-1">
          <span className="text-xs text-muted-foreground">$</span>
          <input
            autoFocus
            type="text"
            inputMode="decimal"
            value={draftInput}
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onCommit();
              if (event.key === "Escape") onCancel();
            }}
            className="w-full bg-transparent font-mono text-sm font-semibold text-foreground outline-none"
            data-testid="paper-earnings-bankroll-input"
          />
          <button
            type="button"
            onClick={onCommit}
            aria-label="Save starting bankroll"
            className="text-positive hover:text-positive/80 focus-visible:ring-focus"
            data-testid="paper-earnings-bankroll-save"
          >
            <Check size={13} />
          </button>
          <button
            type="button"
            onClick={onCancel}
            aria-label="Cancel"
            className="text-muted-foreground hover:text-foreground focus-visible:ring-focus"
          >
            <X size={13} />
          </button>
        </div>
      ) : (
        <p className="mt-1 font-mono text-sm font-semibold" data-testid="paper-earnings-bankroll">
          {fmtDollars(startingBankroll)}
        </p>
      )}
    </div>
  );
}

interface MetricProps {
  label: string;
  value: string;
  note?: string;
  tone?: "default" | "positive" | "negative";
  loading?: boolean;
  testId?: string;
}

function Metric({ label, value, note, tone = "default", loading = false, testId }: MetricProps) {
  return (
    <div className="rounded border border-border px-3 py-2.5">
      <p className="text-[11px] uppercase text-muted-foreground">{label}</p>
      {loading ? (
        <Skeleton className="mt-1 h-5 w-20" />
      ) : (
        <p
          data-testid={testId}
          className={cn(
            "mt-1 font-mono text-sm font-semibold",
            tone === "positive" && "text-positive",
            tone === "negative" && "text-negative",
          )}
        >
          {value}
        </p>
      )}
      {note && <p className="mt-1 text-[10px] text-muted-foreground/80">{note}</p>}
    </div>
  );
}

interface PaperTotals {
  openExposure: number;
  realizedPnl: number;
  openCount: number;
  closedCount: number;
  pendingParlays: number;
  settledParlays: number;
}

function computeTotals(
  positions: PaperPositionRead[],
  parlays: PaperParlayRead[],
): PaperTotals {
  let openExposure = 0;
  let realizedPnl = 0;
  let openCount = 0;
  let closedCount = 0;
  let pendingParlays = 0;
  let settledParlays = 0;

  for (const position of positions) {
    const stake = position.quantity * position.entry_price;
    if (position.status === "open") {
      openExposure += stake;
      openCount += 1;
    } else {
      closedCount += 1;
      if (position.pnl != null) realizedPnl += position.pnl;
    }
  }

  for (const parlay of parlays) {
    // Outcomes: "pending"/"unresolved" are still in-play (locked).
    // Anything else is settled and contributes to realized PnL.
    if (parlay.outcome === "pending" || parlay.outcome === "unresolved") {
      openExposure += parlay.stake;
      pendingParlays += 1;
    } else {
      settledParlays += 1;
      if (parlay.realized_pnl != null) realizedPnl += parlay.realized_pnl;
    }
  }

  return { openExposure, realizedPnl, openCount, closedCount, pendingParlays, settledParlays };
}

function fmtDollars(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function fmtSignedDollars(value: number): string {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${fmtDollars(value)}`;
}
