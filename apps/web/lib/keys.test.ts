import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchEvents, fetchTradeDesk, keys } from "./api";

/**
 * Bug #24: SWR cache keys had two failure modes.
 *
 * (a) ``new URLSearchParams(Object.entries(args))`` preserves whatever
 *     insertion order the caller built ``args`` in. Two logically
 *     identical fetches with the SAME args produced DIFFERENT keys if
 *     the caller built the object in a different field order. SWR
 *     treats those as different keys, doubling fetches and busting
 *     the cache.
 *
 * (b) "All sports" was expressed three ways — ``undefined``, ``""``,
 *     and the literal ``"all"`` (the select widget's value). Any
 *     non-``undefined`` value produced a separate key from
 *     ``tradeDesk()`` / ``events()`` so an explicit "all" selection
 *     refetched while a no-arg call hit cache.
 *
 * The fix sorts the entries before serializing and normalizes the
 * "all sports" aliases to ``undefined`` at the key level.
 */

describe("keys — canonical SWR cache keys (bug #24)", () => {
  describe("predictions / predictionSummary serialize args in sorted order", () => {
    it("returns the same key regardless of caller insertion order", () => {
      const aFirst = keys.predictions({ sport: "NBA", market_family: "winner" });
      const bFirst = keys.predictions({ market_family: "winner", sport: "NBA" });
      expect(aFirst).toBe(bFirst);
    });

    it("drops null / undefined / empty values from the key", () => {
      const sparse = keys.predictions({
        sport: "NBA",
        market_family: undefined,
        stat_key: "",
        outcome: null as unknown as undefined,
      });
      expect(sparse).toBe("/predictions?sport=NBA");
    });

    it("returns the bare path when args is undefined or empty", () => {
      expect(keys.predictions()).toBe("/predictions");
      expect(keys.predictions({})).toBe("/predictions");
    });

    it("uses the same sort rule for predictionSummary", () => {
      const aFirst = keys.predictionSummary({ outcome: "won", sport: "MLB" });
      const bFirst = keys.predictionSummary({ sport: "MLB", outcome: "won" });
      expect(aFirst).toBe(bFirst);
    });
  });

  describe("tradeDesk normalizes all-sports aliases", () => {
    it("treats undefined, empty string, and 'all' as the same key", () => {
      const noArg = keys.tradeDesk();
      const undef = keys.tradeDesk(undefined);
      const empty = keys.tradeDesk("");
      const allLower = keys.tradeDesk("all");
      const allUpper = keys.tradeDesk("ALL");
      expect(undef).toBe(noArg);
      expect(empty).toBe(noArg);
      expect(allLower).toBe(noArg);
      expect(allUpper).toBe(noArg);
    });

    it("keeps a real sport filter on the key", () => {
      expect(keys.tradeDesk("NBA")).toBe("/trade-desk?sport=NBA");
    });
  });

  describe("events normalizes all-sports aliases too", () => {
    it("collapses undefined / empty / 'all' for sport", () => {
      expect(keys.events()).toBe("/events");
      expect(keys.events("")).toBe("/events");
      expect(keys.events("all")).toBe("/events");
    });

    it("preserves an explicit day filter even without sport", () => {
      expect(keys.events(undefined, "2026-05-13")).toBe("/events?day=2026-05-13");
    });

    it("sort order is stable regardless of arg position", () => {
      // Implementation builds the params dict internally; whether
      // 'sport' or 'day' is appended first must NOT affect the
      // resulting key. The serializer sorts entries alphabetically,
      // so 'day' precedes 'sport'.
      expect(keys.events("NBA", "2026-05-13")).toBe(
        "/events?day=2026-05-13&sport=NBA",
      );
    });
  });
});

describe("fetchers also normalize all-sports (codex round-1 P2 on bug #24)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function mockJsonFetch() {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    return fetchSpy;
  }

  it("fetchTradeDesk drops the sport param for 'all'", async () => {
    const fetchSpy = mockJsonFetch();
    await fetchTradeDesk("all");
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/trade-desk",
      expect.any(Object),
    );
  });

  it("fetchTradeDesk drops the sport param for the empty string", async () => {
    const fetchSpy = mockJsonFetch();
    await fetchTradeDesk("");
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/trade-desk",
      expect.any(Object),
    );
  });

  it("fetchTradeDesk preserves a real sport filter", async () => {
    const fetchSpy = mockJsonFetch();
    await fetchTradeDesk("NBA");
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/trade-desk?sport=NBA",
      expect.any(Object),
    );
  });

  it("fetchEvents drops 'all' but keeps an explicit day", async () => {
    const fetchSpy = mockJsonFetch();
    await fetchEvents("all", "2026-05-13");
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/events?day=2026-05-13",
      expect.any(Object),
    );
  });
});
