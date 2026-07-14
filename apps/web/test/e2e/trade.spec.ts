import { expect, test } from "@playwright/test";
import { healthFixture, tradeDeskFixture } from "../fixtures/trade-fixtures";

test("trade uses mocked market data and never requests positions", async ({ page }) => {
  let positionsRequested = false;
  const unexpectedApiRequests: string[] = [];
  const ticketTitle = page.getByTestId("trade-ticket-title").first();

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/health") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(healthFixture),
      });
      return;
    }
    if (url.pathname === "/api/trade-desk") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(tradeDeskFixture),
      });
      return;
    }
    if (url.pathname === "/api/positions") {
      positionsRequested = true;
      await route.abort();
      return;
    }
    // Multi-user PR 2 (#227) — the topbar user switcher fetches these
    // on every page; single-tenant responses keep it rendering nothing.
    if (url.pathname === "/api/me") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ user: null }),
      });
      return;
    }
    if (url.pathname === "/api/users") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
      return;
    }
    if (url.pathname === "/api/product/freshness") {
      // Shell-level freshness banner — not part of /trade regression scope.
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ overall_status: "fresh", scopes: [] }),
      });
      return;
    }
    // Codex round-9 P2 on PR #24: the pick-history strip mounts inside
    // the trade ticket and fires three new endpoints. None of them
    // gate test behavior here — they just need realistic empty
    // payloads so the strip lands in its empty-state and doesn't
    // surface a 500 banner.
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
    if (url.pathname === "/api/research/teams/history") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          entity_id: null,
          team_name: "Toronto Raptors",
          sport_key: "NBA",
          results: [],
        }),
      });
      return;
    }
    if (url.pathname === "/api/research/stats/query") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
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
          summary: {
            games: 0,
            wins: null,
            losses: null,
            draws: null,
            metrics: {},
            stat_line: null,
            percentiles: {},
            metric_categories: {},
          },
          game_logs: [],
          explanation: "",
          source: "espn_public",
        }),
      });
      return;
    }
    unexpectedApiRequests.push(`${url.pathname}${url.search}`);
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: "Unexpected API request during deterministic trade test" }),
    });
  });

  await page.goto("/trade", { waitUntil: "domcontentloaded" });

  // Glass-instrument gauge row — spec 5a testids + values
  await expect(page.getByTestId("trade-gauge-health")).toContainText("7 of 7 scored");
  await expect(page.getByTestId("trade-gauge-avg-edge")).toContainText("+11.2%");
  await expect(page.getByTestId("trade-gauge-top-quartile")).toContainText("+10.0%");
  await expect(page.getByTestId("trade-gauge-events")).toContainText("1 · 0 live");

  await expect(page.getByText("Your Exposure")).toHaveCount(0);
  await expect(page.getByText("Event Context")).toHaveCount(0);

  // Collapsed strip → expand into the featured game panel.
  await page.getByRole("button", { name: /Miami Heat at Toronto Raptors/i }).click();

  const ticketRail = page.getByTestId("trade-ticket-rail");
  const pickRows = page.getByTestId("trade-pick-row");
  await expect(pickRows).toHaveCount(7);
  // Flattened picks sort by edge desc; the hero row is the top pick.
  await expect(pickRows.first()).toContainText("Davion Mitchell 10+ points");

  await pickRows.filter({ hasText: "Toronto Raptors to win" }).click();
  await expect(ticketTitle).toHaveText("Toronto Raptors to win");

  await pickRows.filter({ hasText: "Davion Mitchell 4+ assists" }).click();
  await expect(ticketTitle).toHaveText("Davion Mitchell 4+ assists");
  await expect(ticketRail).toContainText("89.4%");

  await pickRows.filter({ hasText: "Davion Mitchell 10+ points" }).click();
  await expect(ticketTitle).toHaveText("Davion Mitchell 10+ points");
  await expect(ticketRail).toContainText("72.1%");
  await expect(page.locator('[data-testid="trade-pick-row"].selected')).toHaveCount(1);

  expect(positionsRequested).toBe(false);
  expect(unexpectedApiRequests).toEqual([]);
});
