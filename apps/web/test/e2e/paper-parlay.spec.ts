import { expect, test } from "@playwright/test";
import { healthFixture, tradeDeskFixture } from "../fixtures/trade-fixtures";

/**
 * PAPER_PARLAY_SCOPE.md step 8 — end-to-end happy path for the
 * operator-built paper parlay flow.
 *
 * Walks the tray + dialog + portfolio surfaces in one browser session:
 *   1. /trade loads → expand the event → pick a player-prop threshold
 *   2. "Add to parlay" on the ticket → leg 1 in the tray
 *   3. Pick a second threshold → "Add to parlay" → leg 2 in the tray
 *   4. Tray's "Save paper parlay" → dialog opens
 *   5. Type stake → Save → POST /paper-parlays
 *   6. Tray clears, dialog closes
 *   7. Navigate to /positions → Paper Parlays section shows the new row
 *
 * All backend traffic is intercepted by ``page.route`` so the test
 * doesn't depend on a running API. Only the route handlers shipped
 * by paper-parlay steps 1–7 need to be mocked here; the existing
 * tradeDeskFixture covers steps before.
 */

const SAVED_PARLAY = {
  id: 42,
  created_at: "2026-05-17T20:00:00Z",
  stake: 75,
  leg_count: 2,
  sport_scope: "NBA",
  participating_sports: ["NBA"],
  combined_market_price: 0.172,
  combined_model_probability: 0.51,
  american_odds: "+481",
  edge: 0.338,
  notes: null,
  settlement_status: "pending",
  outcome: "pending",
  realized_pnl: null,
  settled_at: null,
  settlement_notes: null,
  legs: [
    {
      id: 1,
      leg_index: 0,
      source_prediction_id: 1,
      market_id: 1,
      ticker: "KXNBAPTS-DAVION-10",
      sport_key: "NBA",
      event_name: "Miami Heat at Toronto Raptors",
      market_title: "Davion Mitchell 10+ points",
      market_kind: "player_prop",
      stat_key: "points",
      threshold: 10,
      subject_name: "Davion Mitchell",
      subject_team: "TOR",
      side: "yes",
      suggested_price: 0.4,
      fair_yes_price: 0.721,
      fair_no_price: 0.279,
    },
    {
      id: 2,
      leg_index: 1,
      source_prediction_id: 2,
      market_id: 2,
      ticker: "KXNBAAST-DAVION-4",
      sport_key: "NBA",
      event_name: "Miami Heat at Toronto Raptors",
      market_title: "Davion Mitchell 4+ assists",
      market_kind: "player_prop",
      stat_key: "assists",
      threshold: 4,
      subject_name: "Davion Mitchell",
      subject_team: "TOR",
      side: "yes",
      suggested_price: 0.85,
      fair_yes_price: 0.894,
      fair_no_price: 0.106,
    },
  ],
};

const POSITIONS_EMPTY = {
  paper_positions: [],
  demo_orders: [],
  kalshi_account: {
    configured: false,
    status: "not_configured",
    error_message: null,
    balance: null,
    market_positions: [],
    recent_fills: [],
  },
  paper_truncated: false,
  demo_truncated: false,
  paper_parlays: [],
  paper_parlays_truncated: false,
  drawdown_brake: null,
};

const POSITIONS_WITH_PARLAY = {
  ...POSITIONS_EMPTY,
  paper_parlays: [SAVED_PARLAY],
};

test("operator builds a 2-leg paper parlay end-to-end", async ({ page }) => {
  // Tray persistence (localStorage) survives across pages — start fresh
  // so previous test runs don't seed a tray into this one.
  await page.addInitScript(() => {
    window.localStorage.removeItem("sika.parlayTray.v1");
  });

  const postedParlayPayloads: unknown[] = [];
  let positionsState = POSITIONS_EMPTY;

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.pathname === "/api/health") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(healthFixture) });
      return;
    }
    if (url.pathname === "/api/trade-desk") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(tradeDeskFixture) });
      return;
    }
    if (url.pathname === "/api/positions") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(positionsState) });
      return;
    }
    if (url.pathname === "/api/paper-parlays" && method === "POST") {
      postedParlayPayloads.push(JSON.parse(route.request().postData() || "{}"));
      // After a successful save, the /positions response surfaces
      // the new parlay (mirrors the real backend flow).
      positionsState = POSITIONS_WITH_PARLAY;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SAVED_PARLAY) });
      return;
    }
    if (url.pathname === "/api/product/freshness") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ overall_status: "fresh", scopes: [] }),
      });
      return;
    }
    if (url.pathname === "/api/ops/models/readiness") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ml_serving_mode: "heuristic",
          fallback_active: false,
          fallback_reason: null,
          family_keys_armed_for_auto_promote: [],
          promotion_stability_days_required: 3,
          pick_history_default_n: 5,
          families: [],
        }),
      });
      return;
    }
    if (
      url.pathname === "/api/research/stats/query" ||
      url.pathname === "/api/research/teams/history"
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ game_logs: [], results: [], summary: {} }),
      });
      return;
    }
    // Fall through: any unmocked endpoint → 404 so we surface
    // accidental new dependencies loudly.
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "Not found in mock" }) });
  });

  await page.goto("/trade");

  // Expand the event row so the player-prop thresholds become clickable.
  await page.getByRole("button", { name: /miami heat at toronto raptors/i }).click();

  // The trade-desk renders TWO ticket instances (desktop sidebar +
  // mobile bottom sheet) — scope locators to the desktop rail so
  // strict-mode selectors don't double-match.
  const ticketRail = page.getByTestId("trade-ticket-rail");

  // Add Davion Mitchell 10+ points to the tray via the ticket flow.
  await page.getByRole("button", { name: "10+" }).first().click();
  await ticketRail.getByTestId("ticket-parlay-toggle").click();
  await expect(page.getByTestId("parlay-tray")).toBeVisible();

  // Add a second leg: 4+ assists for the same player. Same ticket,
  // same player_prop card — just a different threshold.
  await page.getByRole("button", { name: "4+" }).first().click();
  await ticketRail.getByTestId("ticket-parlay-toggle").click();
  const trayChips = page.getByTestId("parlay-tray-chips").locator("li");
  await expect(trayChips).toHaveCount(2);

  // Save: open the dialog, type a stake, submit.
  await page.getByTestId("parlay-tray-save").click();
  await page.getByTestId("paper-parlay-dialog-stake").fill("75");
  await page.getByTestId("paper-parlay-dialog-submit").click();

  // Verify the POST body shape.
  await expect.poll(() => postedParlayPayloads.length).toBe(1);
  expect(postedParlayPayloads[0]).toMatchObject({
    stake: 75,
    legs: [
      { ticker: "KXNBAPTS-DAVION-10", side: "yes" },
      { ticker: "KXNBAAST-DAVION-4", side: "yes" },
    ],
  });

  // Tray clears, dialog closes.
  await expect(page.getByTestId("parlay-tray")).toBeHidden();
  await expect(page.getByTestId("paper-parlay-dialog-submit")).toBeHidden();

  // Navigate to /positions and verify the parlay row appears.
  await page.goto("/positions");
  const parlayRow = page.getByTestId("paper-parlay-row-42");
  await expect(parlayRow).toBeVisible();
  await expect(parlayRow).toContainText("$75.00");
  await expect(page.getByTestId("paper-parlay-status-42")).toHaveText("pending");
});
