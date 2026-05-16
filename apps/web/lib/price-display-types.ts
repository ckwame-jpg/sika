// Server-safe value helpers split out from ``./price-display.tsx``.
//
// ``RootLayout`` is a server component — it reads the price-mode
// cookie before render to seed the provider with the right initial
// value. When ``isPriceDisplayMode`` lived in the client-marked
// ``./price-display.tsx`` module, Next.js 15 rejected the cross-bundle
// import and every page rendered the application error overlay:
//
//     Error: Attempted to call isPriceDisplayMode() from the server
//     but isPriceDisplayMode is on the client.
//
// These three exports are pure values with no React dependency, so
// they're safe to consume from either runtime. The client module
// re-exports them so existing client imports stay unchanged.

export type PriceDisplayMode = "american" | "prediction" | "kalshi";

export const PRICE_DISPLAY_COOKIE = "sika.price-display-mode";

export function isPriceDisplayMode(value: unknown): value is PriceDisplayMode {
  return value === "american" || value === "prediction" || value === "kalshi";
}
