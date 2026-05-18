/**
 * PAPER_PARLAY_SCOPE.md step 5 — module-level subscribable store for
 * the operator-built parlay tray. The tray lives on the trade-desk
 * page and persists across selections, route changes, and (per
 * decision #2) tab refreshes via localStorage, with a 30-minute
 * staleness cap to keep stale market prices from re-hydrating into a
 * future session.
 *
 * Why not Zustand: sika doesn't ship Zustand today, and the surface
 * area we need (legs list + addLeg/removeLeg/clear + a subscription
 * mechanism React can hook into) is small enough that the React 19
 * built-in ``useSyncExternalStore`` covers it without adding a
 * dependency.
 *
 * The store is module-level, not Context-scoped, so any component
 * that calls ``useParlayTray()`` sees the same tray — including the
 * "Add to parlay" button on the trade ticket and the tray itself
 * docked at the bottom of the trade-desk page.
 */

import { useSyncExternalStore } from "react";
import type { TradeSelection } from "@/components/trade/trade-ticket";

const LOCAL_STORAGE_KEY = "sika.parlayTray.v1";

// PAPER_PARLAY_SCOPE.md decision #2 — drop the tray on load if the
// saved-at timestamp is older than 30 minutes. Stops a tab opened
// yesterday from hydrating a parlay with prices that have long since
// drifted from any reality. 30 min matches the trade-desk's
// 30-second refresh × 60 — long enough that a coffee-break tab survives,
// short enough that an overnight tab doesn't.
export const TRAY_STALENESS_CAP_MS = 30 * 60 * 1000;

// PAPER_PARLAY_SCOPE.md — the backend service caps parlays at 6 legs
// (MAX_LEG_COUNT in apps/api/app/services/paper_parlays.py). Enforce
// the same cap on the client so the "Add to parlay" button can
// disable instead of letting the operator queue up a save that's
// guaranteed to 400.
export const MAX_TRAY_LEGS = 6;

interface TrayState {
  legs: TradeSelection[];
  savedAt: number;
}

const INITIAL_STATE: TrayState = { legs: [], savedAt: 0 };

let state: TrayState = INITIAL_STATE;
let hydrated = false;
const listeners = new Set<() => void>();

function hydrateOnce(): void {
  if (hydrated) return;
  hydrated = true;
  if (typeof window === "undefined") return;
  try {
    const raw = window.localStorage.getItem(LOCAL_STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw) as Partial<TrayState>;
    if (
      !parsed ||
      !Array.isArray(parsed.legs) ||
      typeof parsed.savedAt !== "number"
    ) {
      return;
    }
    const age = Date.now() - parsed.savedAt;
    if (age < 0 || age > TRAY_STALENESS_CAP_MS) {
      // Stale (or clock-skewed) — start fresh.
      window.localStorage.removeItem(LOCAL_STORAGE_KEY);
      return;
    }
    state = { legs: parsed.legs as TradeSelection[], savedAt: parsed.savedAt };
  } catch {
    // Corrupt JSON or QuotaExceeded → silently fall back to empty tray.
  }
}

function persist(): void {
  if (typeof window === "undefined") return;
  try {
    if (state.legs.length === 0) {
      window.localStorage.removeItem(LOCAL_STORAGE_KEY);
      return;
    }
    window.localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Private mode / quota — silently swallow; the in-memory tray
    // still works for the session.
  }
}

function setState(next: TrayState): void {
  state = next;
  persist();
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  hydrateOnce();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// Snapshot reads MUST be referentially stable when nothing changed —
// ``useSyncExternalStore`` re-renders whenever the snapshot identity
// shifts. Returning ``state`` (the module-level reference) gives that
// guarantee because ``setState`` always writes a NEW object.
function getSnapshot(): TrayState {
  hydrateOnce();
  return state;
}

// SSR server snapshot. Returning the same INITIAL_STATE reference
// each call avoids hydration mismatches.
function getServerSnapshot(): TrayState {
  return INITIAL_STATE;
}

export interface UseParlayTrayResult {
  legs: TradeSelection[];
  isFull: boolean;
  /** ``true`` when the given selection.ticker is already in the tray. */
  contains: (ticker: string) => boolean;
  addLeg: (selection: TradeSelection) => void;
  removeLeg: (ticker: string) => void;
  clear: () => void;
}

export function useParlayTray(): UseParlayTrayResult {
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  return {
    legs: snapshot.legs,
    isFull: snapshot.legs.length >= MAX_TRAY_LEGS,
    contains: (ticker: string) =>
      snapshot.legs.some((leg) => leg.ticker === ticker),
    addLeg,
    removeLeg,
    clear,
  };
}

// Mutation helpers exposed both via the hook (for components) and
// directly (so non-component callers — e.g. tests, future cron-like
// helpers — don't need to spin up a React tree).

export function addLeg(selection: TradeSelection): void {
  // Idempotent on ticker: if the operator clicks "Add to parlay" twice
  // on the same card, the second click is a no-op rather than a
  // duplicate row. The MAX_TRAY_LEGS guard is checked AFTER the
  // duplicate-check so re-adding an existing ticker doesn't fail just
  // because the tray happens to be full.
  if (state.legs.some((leg) => leg.ticker === selection.ticker)) return;
  if (state.legs.length >= MAX_TRAY_LEGS) return;
  setState({
    legs: [...state.legs, selection],
    savedAt: Date.now(),
  });
}

export function removeLeg(ticker: string): void {
  const next = state.legs.filter((leg) => leg.ticker !== ticker);
  if (next.length === state.legs.length) return;
  setState({ legs: next, savedAt: Date.now() });
}

export function clear(): void {
  if (state.legs.length === 0) return;
  setState({ legs: [], savedAt: 0 });
}

// Test-only helpers — exported under a clearly-namespaced object so a
// future production caller doesn't accidentally tree-shake one of
// these into a real surface.
export const __testing = {
  /** Reset the module-level state. Used between tests so one test's
   *  ``addLeg`` doesn't leak into the next test's ``getSnapshot``. */
  reset: (): void => {
    state = INITIAL_STATE;
    hydrated = false;
    listeners.clear();
    if (typeof window !== "undefined") {
      window.localStorage.removeItem(LOCAL_STORAGE_KEY);
    }
  },
  LOCAL_STORAGE_KEY,
};
