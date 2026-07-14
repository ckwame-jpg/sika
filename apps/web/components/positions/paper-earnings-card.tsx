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

function clampPct(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function useStartingBankroll() {
  const [startingBankroll, setStartingBankroll] = useState<number>(DEFAULT_STARTING_BANKROLL);
  const [hydrated, setHydrated] = useState(false);

  // Hydrate from localStorage on mount. Guarded by ``hydrated`` so the
  // first render doesn't flash the default before the stored value.
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

  function commit(value: number) {
    setStartingBankroll(value);
    try {
      window.localStorage.setItem(LOCAL_STORAGE_KEY, String(value));
    } catch {
      // Quota — keep in-memory value; setting won't survive reload.
    }
  }

  return { startingBankroll, hydrated, commit };
}

function usePaperBuckets() {
  const { data, isLoading } = useSWR<PositionsRead>(keys.positions, fetchPositions);
  // Include the legacy bucket in totals — those bets are still history
  // the operator made, and excluding them gave a misleading "+$0.00
  // realized PnL" even after a -$50 legacy parlay had settled.
  const positions = useMemo(
    () => [...(data?.paper_positions ?? []), ...(data?.legacy_paper_positions ?? [])],
    [data?.paper_positions, data?.legacy_paper_positions],
  );
  const parlays = useMemo(
    () => [...(data?.paper_parlays ?? []), ...(data?.legacy_paper_parlays ?? [])],
    [data?.paper_parlays, data?.legacy_paper_parlays],
  );
  return { positions, parlays, isLoading };
}

/** Spec 5c gauge row: bankroll / at risk / 7d pnl / open-bets orb. */
export function PaperGaugeRow() {
  const { positions, parlays, isLoading } = usePaperBuckets();
  const { startingBankroll, hydrated, commit } = useStartingBankroll();
  const [editing, setEditing] = useState(false);
  const [draftInput, setDraftInput] = useState("");

  const totals = useMemo(() => computeTotals(positions, parlays), [positions, parlays]);
  const week = useMemo(() => computeWindowPnl(positions, parlays, 7), [positions, parlays]);

  const bankroll = startingBankroll + totals.realizedPnl;
  const openBets = totals.openCount + totals.pendingParlays;

  function commitDraft() {
    const value = Number(draftInput.replace(/[$,\s]/g, ""));
    if (Number.isFinite(value) && value > 0) {
      commit(value);
    }
    setEditing(false);
  }

  if (isLoading || !hydrated) {
    return (
      <div className="gi-gauge-row">
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-24 w-full rounded-xl" />
        ))}
      </div>
    );
  }

  return (
    <div className="gi-gauge-row" data-testid="paper-earnings-grid">
      <div className="gi-card gi-gauge-card" data-testid="portfolio-gauge-bankroll">
        <div
          className="gi-gauge"
          style={
            {
              "--gg-p": clampPct((bankroll / Math.max(startingBankroll, 1)) * 50),
              "--gg-c": "var(--color-cosmos-cyan-500)",
            } as React.CSSProperties
          }
          aria-hidden
        >
          <span className="gi-gauge-value">{fmtCompactDollars(bankroll)}</span>
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">
            bankroll
            {!editing && (
              <button
                type="button"
                onClick={() => {
                  setDraftInput(String(startingBankroll));
                  setEditing(true);
                }}
                aria-label="Edit starting bankroll"
                className="ml-1.5 align-middle text-muted-foreground/70 hover:text-foreground focus-visible:ring-focus"
                data-testid="paper-earnings-bankroll-edit"
              >
                <Pencil size={10} />
              </button>
            )}
          </span>
          <span className="gi-gauge-title" data-testid="paper-earnings-bankroll">{fmtDollars(bankroll)}</span>
          {editing ? (
            <span className="flex items-center gap-1">
              <span className="text-xs text-muted-foreground">$</span>
              <input
                autoFocus
                type="text"
                inputMode="decimal"
                value={draftInput}
                onChange={(event) => setDraftInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") commitDraft();
                  if (event.key === "Escape") setEditing(false);
                }}
                className="w-24 bg-transparent font-mono text-xs font-semibold text-foreground outline-none"
                data-testid="paper-earnings-bankroll-input"
              />
              <button
                type="button"
                onClick={commitDraft}
                aria-label="Save starting bankroll"
                className="text-positive hover:text-positive/80 focus-visible:ring-focus"
                data-testid="paper-earnings-bankroll-save"
              >
                <Check size={12} />
              </button>
              <button
                type="button"
                onClick={() => setEditing(false)}
                aria-label="Cancel"
                className="text-muted-foreground hover:text-foreground focus-visible:ring-focus"
              >
                <X size={12} />
              </button>
            </span>
          ) : (
            <span className="gi-gauge-sub">paper · started {fmtDollars(startingBankroll)}</span>
          )}
        </div>
      </div>

      <div className="gi-card gi-gauge-card" data-testid="portfolio-gauge-at-risk">
        <div
          className="gi-gauge"
          style={
            {
              "--gg-p": clampPct((totals.openExposure / Math.max(bankroll, 1)) * 100),
              "--gg-c": "var(--color-cosmos-violet-500)",
            } as React.CSSProperties
          }
          aria-hidden
        >
          <span className="gi-gauge-value">
            {bankroll > 0 ? `${Math.round((totals.openExposure / bankroll) * 100)}%` : "—"}
          </span>
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">at risk</span>
          <span className="gi-gauge-title" data-testid="paper-earnings-open">{fmtDollars(totals.openExposure)}</span>
          <span className="gi-gauge-sub">{openBets} open bet{openBets === 1 ? "" : "s"}</span>
        </div>
      </div>

      <div className="gi-card gi-gauge-card" data-testid="portfolio-gauge-7d">
        <div
          className="gi-gauge"
          style={
            {
              "--gg-p": clampPct(week.winRate != null ? week.winRate * 100 : 0),
              "--gg-c": week.pnl >= 0 ? "var(--gi-green)" : "var(--gi-orange)",
            } as React.CSSProperties
          }
          aria-hidden
        >
          <span className="gi-gauge-value">{week.winRate != null ? `${Math.round(week.winRate * 100)}%` : "—"}</span>
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">7d pnl</span>
          <span
            className="gi-gauge-title"
            style={{ color: week.pnl >= 0 ? "var(--gi-green)" : "var(--gi-orange)" }}
            data-testid="paper-earnings-realized"
          >
            {fmtSignedDollars(week.pnl)}
          </span>
          <span className="gi-gauge-sub">
            {week.settled > 0
              ? `${Math.round((week.winRate ?? 0) * 100)}% win · ${week.settled} settled`
              : "no settles this week"}
          </span>
        </div>
      </div>

      <div className="gi-card gi-gauge-card" data-testid="portfolio-gauge-open">
        <div className="gi-orb-stat" aria-hidden>
          <span className="core" />
        </div>
        <div className="gi-gauge-meta">
          <span className="gi-micro-label">open bets</span>
          <span className="gi-gauge-title">{openBets} open</span>
          <span className="gi-gauge-sub">
            {totals.openCount} single{totals.openCount === 1 ? "" : "s"} · {totals.pendingParlays} parlay
            {totals.pendingParlays === 1 ? "" : "s"}
          </span>
        </div>
      </div>
    </div>
  );
}

/** Spec 5c rail: exposure donut (singles vs parlays), settled today, export. */
export function ExposureRail() {
  const { positions, parlays } = usePaperBuckets();
  const totals = useMemo(() => computeTotals(positions, parlays), [positions, parlays]);

  const singleExposure = positions
    .filter((p) => p.status === "open")
    .reduce((sum, p) => sum + p.quantity * p.entry_price, 0);
  const parlayExposure = parlays
    .filter((p) => p.outcome === "pending" || p.outcome === "unresolved")
    .reduce((sum, p) => sum + p.stake, 0);
  const total = singleExposure + parlayExposure;

  const singlePct = total > 0 ? (singleExposure / total) * 100 : 0;
  const stops =
    total > 0
      ? `var(--color-cosmos-violet-500) 0 ${singlePct}%, var(--color-cosmos-cyan-500) ${singlePct}% 100%`
      : `rgba(255,255,255,.06) 0 100%`;

  const today = new Date().toDateString();
  const settledToday: { key: string; label: string; pnl: number }[] = [
    ...positions
      .filter((p) => p.closed_at && new Date(p.closed_at).toDateString() === today)
      .map((p) => ({
        key: `pos-${p.id}`,
        label: `${p.ticker.toLowerCase()} · ${p.status}`,
        pnl: p.pnl ?? 0,
      })),
    ...parlays
      .filter((p) => p.settled_at && new Date(p.settled_at).toDateString() === today)
      .map((p) => ({
        key: `parlay-${p.id}`,
        label: `${p.leg_count}-leg parlay · ${p.outcome}`,
        pnl: p.realized_pnl ?? 0,
      })),
  ].slice(0, 6);

  function exportLedger() {
    const rows: string[] = [
      "type,id,ticker_or_scope,side,entry_price,quantity_or_stake,status,pnl,opened_at,closed_at",
      ...positions.map((p) =>
        [
          "single",
          p.id,
          p.ticker,
          p.side,
          p.entry_price,
          p.quantity,
          p.status,
          p.pnl ?? "",
          p.opened_at ?? "",
          p.closed_at ?? "",
        ].join(","),
      ),
      ...parlays.map((p) =>
        [
          "parlay",
          p.id,
          p.sport_scope,
          "yes",
          p.combined_market_price,
          p.stake,
          p.outcome,
          p.realized_pnl ?? "",
          p.created_at ?? "",
          p.settled_at ?? "",
        ].join(","),
      ),
    ];
    const blob = new Blob([rows.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `sika-paper-ledger-${new Date().toISOString().slice(0, 10)}.csv`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="gi-rail" data-testid="portfolio-exposure-rail">
      <span className="gi-micro-label rail">exposure by bucket</span>
      <div className="gi-donut" style={{ "--gd-stops": stops } as React.CSSProperties}>
        <span className="gi-donut-ring seg" aria-hidden />
        <span className="gi-donut-orbit" aria-hidden />
        <div className="gi-donut-center">
          <span className="gi-donut-value" style={{ fontSize: 24 }}>{fmtCompactDollars(total)}</span>
          <span className="gi-micro-label">at risk</span>
        </div>
      </div>
      <div className="gi-rail-stat">
        <span className="flex items-center gap-2">
          <span className="gi-glow-dot" style={{ "--gd": "var(--color-cosmos-violet-500)" } as React.CSSProperties} aria-hidden />
          singles
        </span>
        <span className="v">
          {fmtDollars(singleExposure)} · {total > 0 ? Math.round(singlePct) : 0}%
        </span>
      </div>
      <div className="gi-rail-stat">
        <span className="flex items-center gap-2">
          <span className="gi-glow-dot" style={{ "--gd": "var(--color-cosmos-cyan-500)" } as React.CSSProperties} aria-hidden />
          parlays
        </span>
        <span className="v">
          {fmtDollars(parlayExposure)} · {total > 0 ? 100 - Math.round(singlePct) : 0}%
        </span>
      </div>
      <div className="gi-rail-divider" />
      <span className="gi-micro-label rail">settled today</span>
      {settledToday.length === 0 ? (
        <p className="text-[11.5px] text-muted-foreground">nothing settled yet today.</p>
      ) : (
        settledToday.map((row) => (
          <div key={row.key} className="gi-rail-stat">
            <span className="flex min-w-0 items-center gap-2">
              <span
                className="gi-glow-dot"
                style={{ "--gd": row.pnl >= 0 ? "var(--gi-green)" : "var(--gi-orange)" } as React.CSSProperties}
                aria-hidden
              />
              <span className="truncate">{row.label}</span>
            </span>
            <span className={cn("v")} style={{ color: row.pnl >= 0 ? "var(--gi-green)" : "var(--gi-orange)" }}>
              {fmtSignedDollars(row.pnl)}
            </span>
          </div>
        ))
      )}
      <button type="button" className="gi-btn-ghost" onClick={exportLedger} data-testid="portfolio-export-ledger">
        export ledger
      </button>
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

/** Realized PnL + win rate over settles in the trailing N days. */
function computeWindowPnl(
  positions: PaperPositionRead[],
  parlays: PaperParlayRead[],
  days: number,
): { pnl: number; settled: number; winRate: number | null } {
  const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
  let pnl = 0;
  let settled = 0;
  let wins = 0;

  for (const position of positions) {
    if (position.status === "open" || !position.closed_at) continue;
    if (new Date(position.closed_at).getTime() < cutoff) continue;
    settled += 1;
    const value = position.pnl ?? 0;
    pnl += value;
    if (value > 0) wins += 1;
  }
  for (const parlay of parlays) {
    if (parlay.outcome === "pending" || parlay.outcome === "unresolved" || !parlay.settled_at) continue;
    if (new Date(parlay.settled_at).getTime() < cutoff) continue;
    settled += 1;
    const value = parlay.realized_pnl ?? 0;
    pnl += value;
    if (value > 0) wins += 1;
  }

  return { pnl, settled, winRate: settled > 0 ? wins / settled : null };
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

/** "$2.8k" style value for gauge centers (fits 44px disc). */
function fmtCompactDollars(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 10000) return `$${Math.round(value / 1000)}k`;
  if (abs >= 1000) return `$${(value / 1000).toFixed(1)}k`;
  return `$${Math.round(value)}`;
}
