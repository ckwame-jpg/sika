"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";

export type PriceDisplayMode = "american" | "prediction" | "kalshi";

const STORAGE_KEY = "sika.price-display-mode";

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

export function PriceDisplayProvider({ children }: { children: React.ReactNode }) {
  const [mode, setMode] = useState<PriceDisplayMode>("american");

  useEffect(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "american" || stored === "prediction" || stored === "kalshi") {
      setMode(stored);
    }
  }, []);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, mode);
  }, [mode]);

  const value = useMemo<PriceDisplayContextValue>(() => ({
    mode,
    setMode,
    formatPrice: (price) => formatMarketPrice(price, mode),
    formatEditablePrice: (price) => formatEditableMarketPrice(price, mode),
    parsePriceInput: (input) => parseMarketPriceInput(input, mode),
  }), [mode]);

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
