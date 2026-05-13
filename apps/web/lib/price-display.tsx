"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

export type PriceDisplayMode = "american" | "prediction" | "kalshi";

export const PRICE_DISPLAY_COOKIE = "sika.price-display-mode";
const LEGACY_STORAGE_KEY = "sika.price-display-mode";
const COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 365;

export function isPriceDisplayMode(value: unknown): value is PriceDisplayMode {
  return value === "american" || value === "prediction" || value === "kalshi";
}

interface PriceDisplayContextValue {
  mode: PriceDisplayMode;
  setMode: (mode: PriceDisplayMode) => void;
  formatPrice: (price: number | null | undefined) => string;
  formatEditablePrice: (price: number | null | undefined) => string;
  parsePriceInput: (input: string) => number | null;
}

const PriceDisplayContext = createContext<PriceDisplayContextValue | null>(null);

function americanOddsFromPrice(price: number | null | undefined): string {
  if (price == null) return "—";
  const value = Math.min(0.99, Math.max(0.01, price));
  if (value >= 0.5) {
    return `${-Math.round((value / (1 - value)) * 100)}`;
  }
  return `+${Math.round(((1 - value) / value) * 100)}`;
}

export function formatMarketPrice(price: number | null | undefined, mode: PriceDisplayMode): string {
  if (price == null) return "—";
  if (mode === "american") return americanOddsFromPrice(price);
  if (mode === "prediction") return `${(price * 100).toFixed(1)}%`;
  return `${Math.round(price * 100)}¢`;
}

function formatEditableMarketPrice(price: number | null | undefined, mode: PriceDisplayMode): string {
  if (price == null) return "";
  if (mode === "american") return americanOddsFromPrice(price);
  if (mode === "prediction") return (price * 100).toFixed(1);
  return (price * 100).toFixed(0);
}

function parseMarketPriceInput(input: string, mode: PriceDisplayMode): number | null {
  const normalized = input.trim().replace(/¢/g, "").replace(/%/g, "");
  if (!normalized) return null;
  const value = Number.parseFloat(normalized);
  if (!Number.isFinite(value)) return null;

  if (mode === "american") {
    if (value === 0) return null;
    if (value > 0) return Math.min(0.99, Math.max(0.01, 100 / (value + 100)));
    const magnitude = Math.abs(value);
    return Math.min(0.99, Math.max(0.01, magnitude / (magnitude + 100)));
  }

  if (mode === "prediction") {
    const pct = value > 1 ? value / 100 : value;
    return pct > 0 && pct < 1 ? pct : null;
  }

  const cents = value > 1 ? value / 100 : value;
  return cents > 0 && cents < 1 ? cents : null;
}

function readModeFromDocumentCookie(): PriceDisplayMode | null {
  if (typeof document === "undefined") return null;
  const cookies = document.cookie ? document.cookie.split(";") : [];
  for (const entry of cookies) {
    const [rawName, ...rest] = entry.split("=");
    if (rawName?.trim() !== PRICE_DISPLAY_COOKIE) continue;
    const value = decodeURIComponent(rest.join("=").trim());
    if (isPriceDisplayMode(value)) return value;
  }
  return null;
}

function writeModeToDocumentCookie(mode: PriceDisplayMode): void {
  if (typeof document === "undefined") return;
  const value = encodeURIComponent(mode);
  document.cookie = `${PRICE_DISPLAY_COOKIE}=${value}; path=/; max-age=${COOKIE_MAX_AGE_SECONDS}; samesite=lax`;
}

function readLegacyLocalStorage(): PriceDisplayMode | null {
  try {
    const stored = window.localStorage.getItem(LEGACY_STORAGE_KEY);
    return isPriceDisplayMode(stored) ? stored : null;
  } catch {
    return null;
  }
}

function clearLegacyLocalStorage(): void {
  try {
    window.localStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch {
    // Best-effort cleanup; safe to ignore quota / privacy-mode errors.
  }
}

interface PriceDisplayProviderProps {
  children: React.ReactNode;
  initialMode?: PriceDisplayMode;
}

export function PriceDisplayProvider({ children, initialMode }: PriceDisplayProviderProps) {
  const [mode, setModeState] = useState<PriceDisplayMode>(initialMode ?? "american");
  // Bug #36 — track first-mount migration separately from explicit
  // setMode calls. Persisting only on real intent (user toggle or
  // legacy migration) avoids echoing a default value back into the
  // cookie before the legacy-localStorage read has had a chance to
  // run.
  const migratedRef = useRef(false);

  const setMode = useCallback((next: PriceDisplayMode) => {
    setModeState(next);
    writeModeToDocumentCookie(next);
  }, []);

  useEffect(() => {
    if (migratedRef.current) return;
    migratedRef.current = true;

    if (initialMode) {
      // Server saw the cookie on this request; the cookie is now the
      // source of truth. Drop any leftover legacy entry so we don't
      // leak state across two stores.
      clearLegacyLocalStorage();
      return;
    }

    const cookieMode = readModeFromDocumentCookie();
    if (cookieMode) {
      clearLegacyLocalStorage();
      if (cookieMode !== mode) setModeState(cookieMode);
      return;
    }

    const legacyMode = readLegacyLocalStorage();
    if (legacyMode) {
      writeModeToDocumentCookie(legacyMode);
      clearLegacyLocalStorage();
      if (legacyMode !== mode) setModeState(legacyMode);
    }
  }, [initialMode, mode]);

  const value = useMemo<PriceDisplayContextValue>(() => ({
    mode,
    setMode,
    formatPrice: (price) => formatMarketPrice(price, mode),
    formatEditablePrice: (price) => formatEditableMarketPrice(price, mode),
    parsePriceInput: (input) => parseMarketPriceInput(input, mode),
  }), [mode, setMode]);

  return (
    <PriceDisplayContext.Provider value={value}>
      {children}
    </PriceDisplayContext.Provider>
  );
}

export function usePriceDisplay() {
  const context = useContext(PriceDisplayContext);
  if (!context) throw new Error("usePriceDisplay must be used within PriceDisplayProvider");
  return context;
}
