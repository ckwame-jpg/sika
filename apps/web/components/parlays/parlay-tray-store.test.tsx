import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { TradeSelection } from "@/components/trade/trade-ticket";
import {
  __testing,
  addLeg as addLegDirect,
  MAX_TRAY_LEGS,
  TRAY_STALENESS_CAP_MS,
  useParlayTray,
} from "./parlay-tray-store";

function makeLeg(ticker: string, overrides: Partial<TradeSelection> = {}): TradeSelection {
  return {
    kind: "player_prop",
    ticker,
    eventId: 1,
    marketTitle: `${ticker} 25+ points`,
    eventName: "Cleveland Cavaliers at Detroit Pistons",
    sportKey: "NBA",
    marketKind: "player_prop",
    displayLabel: `${ticker} 25+ points`,
    projectedSideLabel: null,
    selectedSide: "yes",
    selectedSideProbability: 0.62,
    entryPrice: 0.55,
    edge: 0.27,
    confidence: 0.85,
    kalshiUrl: null,
    subjectName: "Test Player",
    subjectTeam: "CLE",
    statKey: "points",
    threshold: 25,
    ...overrides,
  };
}

beforeEach(() => {
  __testing.reset();
});

describe("useParlayTray — basics", () => {
  it("starts empty and exposes addLeg / removeLeg / clear", () => {
    const { result } = renderHook(() => useParlayTray());
    expect(result.current.legs).toEqual([]);
    expect(result.current.isFull).toBe(false);
    expect(result.current.contains("ANY")).toBe(false);
  });

  it("addLeg appends a new leg and re-renders subscribers", () => {
    const { result } = renderHook(() => useParlayTray());
    act(() => result.current.addLeg(makeLeg("A")));
    expect(result.current.legs.map((leg) => leg.ticker)).toEqual(["A"]);
    expect(result.current.contains("A")).toBe(true);
  });

  it("addLeg is idempotent on ticker (no duplicates)", () => {
    const { result } = renderHook(() => useParlayTray());
    act(() => {
      result.current.addLeg(makeLeg("A"));
      result.current.addLeg(makeLeg("A"));
    });
    expect(result.current.legs).toHaveLength(1);
  });

  it("removeLeg removes the matching ticker", () => {
    const { result } = renderHook(() => useParlayTray());
    act(() => {
      result.current.addLeg(makeLeg("A"));
      result.current.addLeg(makeLeg("B"));
      result.current.removeLeg("A");
    });
    expect(result.current.legs.map((leg) => leg.ticker)).toEqual(["B"]);
  });

  it("clear empties the tray", () => {
    const { result } = renderHook(() => useParlayTray());
    act(() => {
      result.current.addLeg(makeLeg("A"));
      result.current.addLeg(makeLeg("B"));
      result.current.clear();
    });
    expect(result.current.legs).toEqual([]);
  });

  it("isFull flips when the tray reaches MAX_TRAY_LEGS", () => {
    const { result } = renderHook(() => useParlayTray());
    act(() => {
      for (let i = 0; i < MAX_TRAY_LEGS; i += 1) {
        result.current.addLeg(makeLeg(`L${i}`));
      }
    });
    expect(result.current.legs).toHaveLength(MAX_TRAY_LEGS);
    expect(result.current.isFull).toBe(true);
  });

  it("addLeg is a no-op when the tray is already full", () => {
    const { result } = renderHook(() => useParlayTray());
    act(() => {
      for (let i = 0; i < MAX_TRAY_LEGS; i += 1) {
        result.current.addLeg(makeLeg(`L${i}`));
      }
      result.current.addLeg(makeLeg("OVERFLOW"));
    });
    expect(result.current.legs).toHaveLength(MAX_TRAY_LEGS);
    expect(result.current.contains("OVERFLOW")).toBe(false);
  });
});

describe("useParlayTray — localStorage persistence (decision #2)", () => {
  it("persists adds to localStorage", () => {
    addLegDirect(makeLeg("PERSIST"));
    const raw = window.localStorage.getItem(__testing.LOCAL_STORAGE_KEY);
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.legs).toHaveLength(1);
    expect(parsed.legs[0].ticker).toBe("PERSIST");
    expect(typeof parsed.savedAt).toBe("number");
  });

  it("clear removes the localStorage key entirely", () => {
    addLegDirect(makeLeg("X"));
    expect(window.localStorage.getItem(__testing.LOCAL_STORAGE_KEY)).not.toBeNull();
    __testing.reset();
    expect(window.localStorage.getItem(__testing.LOCAL_STORAGE_KEY)).toBeNull();
  });

  it("hydrates a fresh tray from localStorage on first read", () => {
    // Seed storage as if a previous session had saved a tray.
    window.localStorage.setItem(
      __testing.LOCAL_STORAGE_KEY,
      JSON.stringify({
        legs: [makeLeg("HYDRATED")],
        savedAt: Date.now() - 5000,
      }),
    );
    // Reset in-memory state but keep storage.
    const restoreRaw = window.localStorage.getItem(__testing.LOCAL_STORAGE_KEY);
    __testing.reset();
    // Re-seed because reset clears storage.
    window.localStorage.setItem(__testing.LOCAL_STORAGE_KEY, restoreRaw!);

    const { result } = renderHook(() => useParlayTray());
    expect(result.current.legs).toHaveLength(1);
    expect(result.current.legs[0].ticker).toBe("HYDRATED");
  });

  it("drops the tray when the saved snapshot is older than the staleness cap", () => {
    // Codex pattern 5 / 6: stale snapshots can resurrect a tray
    // whose market prices have long since drifted. The 30-min cap
    // catches the overnight-tab case.
    const tooOld = Date.now() - TRAY_STALENESS_CAP_MS - 1000;
    window.localStorage.setItem(
      __testing.LOCAL_STORAGE_KEY,
      JSON.stringify({
        legs: [makeLeg("STALE")],
        savedAt: tooOld,
      }),
    );
    const restoreRaw = window.localStorage.getItem(__testing.LOCAL_STORAGE_KEY);
    __testing.reset();
    window.localStorage.setItem(__testing.LOCAL_STORAGE_KEY, restoreRaw!);

    const { result } = renderHook(() => useParlayTray());
    expect(result.current.legs).toEqual([]);
    // Storage was also cleaned up.
    expect(window.localStorage.getItem(__testing.LOCAL_STORAGE_KEY)).toBeNull();
  });

  it("handles corrupt localStorage payload gracefully", () => {
    window.localStorage.setItem(__testing.LOCAL_STORAGE_KEY, "{not valid json");
    const restoreRaw = window.localStorage.getItem(__testing.LOCAL_STORAGE_KEY);
    __testing.reset();
    window.localStorage.setItem(__testing.LOCAL_STORAGE_KEY, restoreRaw!);

    const { result } = renderHook(() => useParlayTray());
    expect(result.current.legs).toEqual([]);
  });

  it("survives a localStorage write that throws (e.g. QuotaExceeded)", () => {
    const originalSetItem = window.localStorage.setItem;
    const setItemSpy = vi
      .spyOn(window.localStorage.__proto__, "setItem")
      .mockImplementation(() => {
        throw new Error("QuotaExceededError");
      });
    try {
      const { result } = renderHook(() => useParlayTray());
      // Should not throw — the in-memory tray still works even if
      // persistence fails.
      expect(() => act(() => result.current.addLeg(makeLeg("Q")))).not.toThrow();
      expect(result.current.legs.map((leg) => leg.ticker)).toEqual(["Q"]);
    } finally {
      setItemSpy.mockRestore();
      window.localStorage.setItem = originalSetItem;
    }
  });
});
