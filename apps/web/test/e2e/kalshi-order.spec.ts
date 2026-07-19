/**
 * Real Kalshi order placement — singles + combos — with every /api/*
 * call intercepted (no backend, no Kalshi, no real money; the POST
 * bodies are captured and asserted instead).
 *
 * Flow under test mirrors the user's: connected demo credentials →
 * amber "place on kalshi · demo" affordances → form → confirm →
 * captured payload.
 */

import { expect, test } from "@playwright/test";
import { healthFixture, tradeDeskFixture } from "../fixtures/trade-fixtures";

const DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2";

interface CapturedRequests {
  kalshiOrders: unknown[];
  kalshiCombos: unknown[];
  comboPreviews: unknown[];
  unexpected: string[];
}

async function routeApi(page: import("@playwright/test").Page): Promise<CapturedRequests> {
  const captured: CapturedRequests = {
    kalshiOrders: [],
    kalshiCombos: [],
    comboPreviews: [],
    unexpected: [],
  };

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

    switch (url.pathname) {
      case "/api/health":
        return json(healthFixture);
      case "/api/trade-desk":
        return json(tradeDeskFixture);
      case "/api/me":
        return json({ user: { id: 1, username: "chris", display_name: "chris", is_kalshi_owner: true } });
      case "/api/users":
        return json([{ id: 1, username: "chris", display_name: "chris", is_kalshi_owner: true }]);
      case "/api/me/kalshi-credentials":
        return json({ configured: true, key_id: "k-e2e", base_url: DEMO_URL, updated_at: null });
      case "/api/settings/trading":
        return json({ max_order_cost_dollars: 25 });
      case "/api/kalshi-orders":
        if (route.request().method() === "POST") {
          const body = route.request().postDataJSON();
          captured.kalshiOrders.push(body);
          return json({
            id: 1,
            kind: "single",
            ticker: body.ticker,
            environment: "demo",
            client_order_id: "c-e2e-1",
            kalshi_order_id: null,
            side: body.side,
            action: body.action,
            quantity: body.quantity,
            limit_price: body.limit_price,
            status: "submitting",
            collection_ticker: null,
            combo_event_ticker: null,
            approved_by_user: true,
            error_detail: null,
            created_at: "2026-07-19T18:00:00Z",
            submitted_at: null,
            last_synced_at: null,
            legs: [],
            fills: [],
          });
        }
        return json([]);
      case "/api/kalshi-combos/preview": {
        const body = route.request().postDataJSON();
        captured.comboPreviews.push(body);
        return json({
          combinable: true,
          reason: null,
          collection_ticker: "KXNBACOMBO",
          existing_market_ticker: "KXCOMBO-EXISTING",
          implied_price: 0.3,
          quote_yes_bid: 0.18,
          quote_yes_ask: 0.22,
        });
      }
      case "/api/kalshi-combos": {
        const body = route.request().postDataJSON();
        captured.kalshiCombos.push(body);
        return json({
          id: 2,
          kind: "combo",
          ticker: null,
          environment: "demo",
          client_order_id: "c-e2e-2",
          kalshi_order_id: null,
          side: "yes",
          action: "buy",
          quantity: body.quantity,
          limit_price: body.limit_price,
          status: "submitting",
          collection_ticker: "KXNBACOMBO",
          combo_event_ticker: null,
          approved_by_user: true,
          error_detail: null,
          created_at: "2026-07-19T18:00:00Z",
          submitted_at: null,
          last_synced_at: null,
          legs: [],
          fills: [],
        });
      }
      case "/api/positions":
        return json({
          positions: [],
          demo_orders: [],
          paper_parlays: [],
          legacy: null,
          drawdown_brake: null,
          kalshi_account: { configured: false, status: "not_configured", error_message: null, balance: null, market_positions: [], recent_fills: [] },
        });
      case "/api/product/freshness":
        return json({ overall_status: "fresh", scopes: [] });
      case "/api/ops/models/readiness":
        return json({
          ml_serving_mode: "heuristic",
          fallback_active: false,
          fallback_reason: null,
          family_keys_armed_for_auto_promote: [],
          promotion_stability_days_required: 3,
          pick_history_default_n: 5,
          families: [],
        });
      case "/api/research/teams/history":
        return json({ entity_id: null, team_name: "Toronto Raptors", sport_key: "NBA", results: [] });
      case "/api/research/stats/query":
        return json({
          question: "",
          sport_key: "NBA",
          entity_name: "Davion Mitchell",
          entity_id: null,
          team_name: "Toronto Raptors",
          query_type: "last_n_games",
          season: 2026,
          games_requested: 5,
          games_analyzed: 0,
          split: null,
          opponent: null,
          metric_labels: {},
          summary: { games: 0, wins: null, losses: null, draws: null, metrics: {}, stat_line: null, percentiles: {}, metric_categories: {} },
          game_logs: [],
          explanation: "",
          source: "espn_public",
        });
      default:
        captured.unexpected.push(`${url.pathname}${url.search}`);
        return json({ error: "unexpected request in kalshi-order e2e" }, 500);
    }
  });

  return captured;
}

test("places a real single order through the confirm flow", async ({ page }) => {
  const captured = await routeApi(page);
  await page.goto("/trade", { waitUntil: "domcontentloaded" });

  // Preloaded hero ticket + the amber live affordance (demo-labeled).
  const liveButton = page.getByTestId("ticket-place-on-kalshi").first();
  await expect(liveButton).toHaveText("place on kalshi · demo");
  await liveButton.click();

  await expect(page.getByTestId("kalshi-order-env-badge")).toContainText("demo / sandbox");

  // $10 quick-stake; fixture hero price 40¢ → 25 contracts.
  await page.getByText("$10", { exact: true }).click();
  await expect(page.getByTestId("kalshi-order-preview")).toContainText("25 contracts");

  await page.getByTestId("kalshi-order-review").click();
  const summary = page.getByTestId("kalshi-order-confirm-summary");
  await expect(summary).toContainText("Davion Mitchell 10+ points");
  await expect(summary).toContainText("Total cost$10.00");
  await expect(summary).toContainText("Per-order cap$25");

  await page.getByTestId("kalshi-order-confirm").click();
  await expect
    .poll(() => captured.kalshiOrders.length, { message: "order POST captured" })
    .toBe(1);
  expect(captured.kalshiOrders[0]).toMatchObject({
    ticker: "KXNBAPTS-DAVION-10",
    side: "yes",
    action: "buy",
    quantity: 25,
    limit_price: 0.4,
    approved: true,
    time_in_force: "good_till_canceled",
  });
  expect(captured.unexpected).toEqual([]);
});

test("builds a tray combo and places it as a real kalshi combo", async ({ page }) => {
  const captured = await routeApi(page);
  await page.goto("/trade", { waitUntil: "domcontentloaded" });

  const pickRows = page.getByTestId("trade-pick-row");
  const ticketRail = page.getByTestId("trade-ticket-rail");

  // Two legs into the tray via the ticket's "+ parlay" toggle.
  await pickRows.filter({ hasText: "Davion Mitchell 10+ points" }).click();
  await ticketRail.getByTestId("ticket-parlay-toggle").click();
  await pickRows.filter({ hasText: "Davion Mitchell 4+ assists" }).click();
  await ticketRail.getByTestId("ticket-parlay-toggle").click();

  // Debounced combinability check resolves against the mocked preview.
  await expect(page.getByTestId("parlay-tray-combinability")).toContainText(
    "combinable on kalshi ✓ · live combo market exists · ask 22¢",
    { timeout: 5000 },
  );
  expect(captured.comboPreviews[0]).toMatchObject({
    legs: [
      expect.objectContaining({ ticker: "KXNBAPTS-DAVION-10", side: "yes" }),
      expect.objectContaining({ ticker: "KXNBAAST-DAVION-4", side: "yes" }),
    ],
  });

  const placeCombo = page.getByTestId("parlay-tray-place-kalshi");
  await expect(placeCombo).toBeEnabled();
  await placeCombo.click();

  await expect(page.getByTestId("kalshi-combo-legs")).toContainText("Davion Mitchell 10+ points");
  await page.getByText("$5", { exact: true }).click();
  // $5 @ 22¢ (live ask prefill) → 23 contracts.
  await expect(page.getByTestId("kalshi-combo-preview-line")).toContainText("23 contracts");

  await page.getByTestId("kalshi-combo-review").click();
  await expect(page.getByTestId("kalshi-combo-confirm-summary")).toContainText(
    "Pays if ALL legs hit$23.00",
  );

  await page.getByTestId("kalshi-combo-confirm").click();
  await expect
    .poll(() => captured.kalshiCombos.length, { message: "combo POST captured" })
    .toBe(1);
  expect(captured.kalshiCombos[0]).toMatchObject({
    quantity: 23,
    limit_price: 0.22,
    approved: true,
    time_in_force: "good_till_canceled",
    legs: [
      expect.objectContaining({ ticker: "KXNBAPTS-DAVION-10", side: "yes", entry_price: 0.4 }),
      expect.objectContaining({ ticker: "KXNBAAST-DAVION-4", side: "yes" }),
    ],
  });

  // Tray cleared — the legs became a real order.
  await expect(page.getByTestId("parlay-tray")).toHaveCount(0);
  expect(captured.unexpected).toEqual([]);
});
