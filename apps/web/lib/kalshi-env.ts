/**
 * Kalshi environment constants — single source of truth for the
 * prod/demo base URLs used by the settings env select, the live-order
 * affordances (badge/labels), and anywhere else that needs to know
 * whether the user's stored base_url is the sandbox.
 */

export const KALSHI_PROD_URL = "https://api.elections.kalshi.com/trade-api/v2";
export const KALSHI_DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2";

export function isKalshiDemoUrl(baseUrl: string | null | undefined): boolean {
  if (!baseUrl) return false;
  return baseUrl.replace(/\/+$/, "") === KALSHI_DEMO_URL.replace(/\/+$/, "");
}

/** "live" | "demo" label for a stored base_url (anything non-sandbox is live). */
export function kalshiEnvLabel(baseUrl: string | null | undefined): "live" | "demo" {
  return isKalshiDemoUrl(baseUrl) ? "demo" : "live";
}
