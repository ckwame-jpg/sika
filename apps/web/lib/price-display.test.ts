import { createElement, type ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  PRICE_DISPLAY_COOKIE,
  type PriceDisplayMode,
  PriceDisplayProvider,
  formatMarketPrice,
  isPriceDisplayMode,
  usePriceDisplay,
} from "./price-display";

const LEGACY_KEY = "sika.price-display-mode";

function clearCookie(name: string): void {
  document.cookie = `${name}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
}

function readCookie(name: string): string | null {
  const cookies = document.cookie ? document.cookie.split(";") : [];
  for (const entry of cookies) {
    const [rawName, ...rest] = entry.split("=");
    if (rawName?.trim() === name) return decodeURIComponent(rest.join("=").trim());
  }
  return null;
}

function makeWrapper(initialMode?: PriceDisplayMode) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(PriceDisplayProvider, { initialMode, children });
  };
}

describe("isPriceDisplayMode", () => {
  it.each(["american", "prediction", "kalshi"])("accepts %s", (value) => {
    expect(isPriceDisplayMode(value)).toBe(true);
  });

  it.each([null, undefined, "", "decimal", 42, {}])("rejects %p", (value) => {
    expect(isPriceDisplayMode(value)).toBe(false);
  });
});

describe("formatMarketPrice", () => {
  it("renders american odds favorite below 50%", () => {
    expect(formatMarketPrice(0.6, "american")).toBe("-150");
  });

  it("renders prediction percent with one decimal", () => {
    expect(formatMarketPrice(0.5234, "prediction")).toBe("52.3%");
  });

  it("renders kalshi cents rounded", () => {
    expect(formatMarketPrice(0.5234, "kalshi")).toBe("52¢");
  });

  it("returns em-dash for null", () => {
    expect(formatMarketPrice(null, "american")).toBe("—");
  });
});

describe("PriceDisplayProvider", () => {
  beforeEach(() => {
    clearCookie(PRICE_DISPLAY_COOKIE);
    window.localStorage.clear();
  });

  afterEach(() => {
    clearCookie(PRICE_DISPLAY_COOKIE);
    window.localStorage.clear();
  });

  it("uses initialMode from server when provided", () => {
    const { result } = renderHook(() => usePriceDisplay(), { wrapper: makeWrapper("prediction") });
    expect(result.current.mode).toBe("prediction");
  });

  it("does not write a cookie before any setMode when initialMode is server-supplied", async () => {
    // The provider should only persist after a real mode change so we
    // never echo the server-known value back into the cookie unless
    // the user actually picked a different one.
    const { result } = renderHook(() => usePriceDisplay(), { wrapper: makeWrapper("kalshi") });
    expect(result.current.mode).toBe("kalshi");
    await waitFor(() => {
      expect(readCookie(PRICE_DISPLAY_COOKIE)).toBeNull();
    });
  });

  it("hydrates from a client-side cookie when no initialMode is provided", async () => {
    document.cookie = `${PRICE_DISPLAY_COOKIE}=prediction; path=/`;
    const { result } = renderHook(() => usePriceDisplay(), { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(result.current.mode).toBe("prediction");
    });
  });

  it("migrates legacy localStorage value into the cookie and clears storage", async () => {
    window.localStorage.setItem(LEGACY_KEY, "kalshi");
    const { result } = renderHook(() => usePriceDisplay(), { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(result.current.mode).toBe("kalshi");
    });
    expect(readCookie(PRICE_DISPLAY_COOKIE)).toBe("kalshi");
    expect(window.localStorage.getItem(LEGACY_KEY)).toBeNull();
  });

  it("ignores a corrupt cookie value", async () => {
    document.cookie = `${PRICE_DISPLAY_COOKIE}=fractional; path=/`;
    const { result } = renderHook(() => usePriceDisplay(), { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(result.current.mode).toBe("american");
    });
  });

  it("ignores a corrupt legacy localStorage value", async () => {
    window.localStorage.setItem(LEGACY_KEY, "decimal");
    const { result } = renderHook(() => usePriceDisplay(), { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(result.current.mode).toBe("american");
    });
    // Corrupt legacy entry stays in place — we only clear it after a
    // successful migration, so a future code path could still
    // diagnose it.
    expect(window.localStorage.getItem(LEGACY_KEY)).toBe("decimal");
  });

  it("persists setMode updates to the cookie", async () => {
    const { result } = renderHook(() => usePriceDisplay(), { wrapper: makeWrapper("american") });
    act(() => {
      result.current.setMode("prediction");
    });
    await waitFor(() => {
      expect(readCookie(PRICE_DISPLAY_COOKIE)).toBe("prediction");
    });
  });

  it("clears legacy localStorage when initialMode came from the server", async () => {
    window.localStorage.setItem(LEGACY_KEY, "kalshi");
    renderHook(() => usePriceDisplay(), { wrapper: makeWrapper("prediction") });
    await waitFor(() => {
      expect(window.localStorage.getItem(LEGACY_KEY)).toBeNull();
    });
  });

  it("clears legacy localStorage when a client-side cookie wins", async () => {
    window.localStorage.setItem(LEGACY_KEY, "kalshi");
    document.cookie = `${PRICE_DISPLAY_COOKIE}=prediction; path=/`;
    renderHook(() => usePriceDisplay(), { wrapper: makeWrapper() });
    await waitFor(() => {
      expect(window.localStorage.getItem(LEGACY_KEY)).toBeNull();
    });
  });

  it("does not clobber an existing cookie with the default before migration runs", () => {
    // Without the initializedRef gate, the persistence effect would
    // run synchronously after first paint with the default mode and
    // overwrite the user's stored cookie before the read effect
    // could pick it up.
    document.cookie = `${PRICE_DISPLAY_COOKIE}=prediction; path=/`;
    renderHook(() => usePriceDisplay(), { wrapper: makeWrapper() });
    expect(readCookie(PRICE_DISPLAY_COOKIE)).toBe("prediction");
  });
});
