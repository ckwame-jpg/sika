import { expect, test } from "@playwright/test";
import { healthFixture } from "../fixtures/trade-fixtures";

// PR 3c regression: Stats Assistant must render the AdvancedMetricsGrid with
// percentile bars, color tones, and progressbar a11y when the API returns
// advanced metric_categories. Locking the wire shape here means future
// changes to /research/stats/query that drop or rename `metric_categories` /
// `percentiles` / advanced metric keys will fail this spec instead of being
// caught by hand during a touch test.

const NBA_STATS_QUERY_RESPONSE = {
  question: "Jalen Brunson last 10 games",
  sport_key: "NBA",
  entity_name: "Jalen Brunson",
  entity_id: "1234",
  team_name: "New York Knicks",
  query_type: "last_n_games",
  season: 2026,
  games_requested: 10,
  games_analyzed: 10,
  split: null,
  opponent: null,
  metric_labels: {
    points: "Points",
    rebounds: "Rebounds",
    assists: "Assists",
    ts_pct: "TS%",
    usg_pct: "USG%",
    off_rating: "ORtg",
    def_rating: "DRtg",
  },
  summary: {
    games: 10,
    wins: 6,
    losses: 4,
    draws: 0,
    metrics: {
      points: 28.5,
      rebounds: 4.0,
      assists: 7.2,
      ts_pct: 0.612,
      usg_pct: 0.32,
      off_rating: 118.0,
      def_rating: 108.0,
    },
    stat_line: "28.5 points, 7.2 assists, 4 rebounds",
    percentiles: {
      ts_pct: 82,
      usg_pct: 91,
      off_rating: 75,
      def_rating: 65,
    },
    metric_categories: {
      points: "basic",
      rebounds: "basic",
      assists: "basic",
      ts_pct: "advanced",
      usg_pct: "advanced",
      off_rating: "advanced",
      def_rating: "advanced",
    },
  },
  game_logs: [],
};

test("stats assistant renders AdvancedMetricsGrid with percentile bars (PR 3c)", async ({
  page,
}) => {
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
    if (url.pathname === "/api/research/stats/query") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(NBA_STATS_QUERY_RESPONSE),
      });
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
    // Pass-through other API calls as 200 empty so the page can render.
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "{}",
    });
  });

  await page.goto("/stats", { waitUntil: "domcontentloaded" });

  await page.getByTestId("sa-input").fill("Jalen Brunson last 10 games");
  await page.getByTestId("sa-run").click();

  // Result section populates (not the empty/loading placeholder).
  await expect(page.getByTestId("sa-answer")).toBeVisible({ timeout: 10_000 });

  // Basic metrics render.
  await expect(page.getByTestId("sa-metric-points")).toBeVisible();
  await expect(page.getByTestId("sa-metric-rebounds")).toBeVisible();
  await expect(page.getByTestId("sa-metric-assists")).toBeVisible();

  // Advanced grid renders with all four advanced rows from the mock.
  const advancedGrid = page.getByTestId("sa-advanced-grid");
  await expect(advancedGrid).toBeVisible();
  await expect(page.getByTestId("sa-advanced-ts_pct")).toBeVisible();
  await expect(page.getByTestId("sa-advanced-usg_pct")).toBeVisible();
  await expect(page.getByTestId("sa-advanced-off_rating")).toBeVisible();
  await expect(page.getByTestId("sa-advanced-def_rating")).toBeVisible();

  // PercentileBar a11y contract: role="progressbar" with valuenow/min/max.
  const tsPctBar = page
    .getByTestId("sa-advanced-ts_pct")
    .getByRole("progressbar");
  await expect(tsPctBar).toHaveAttribute("aria-valuenow", "82");
  await expect(tsPctBar).toHaveAttribute("aria-valuemin", "0");
  await expect(tsPctBar).toHaveAttribute("aria-valuemax", "100");

  // Color tone class follows the percentile (>66 → "is-high", 33-66 → "is-mid").
  await expect(tsPctBar).toHaveClass(/is-high/);
  const dRtgBar = page
    .getByTestId("sa-advanced-def_rating")
    .getByRole("progressbar");
  await expect(dRtgBar).toHaveClass(/is-mid/);
});

test("stats assistant gracefully handles empty advanced data (PR 3c fallback)", async ({
  page,
}) => {
  // Same shape but no advanced metrics — verifies the basic-only path doesn't
  // render the AdvancedMetricsGrid (component returns null when no advanced
  // keys are present).
  const basicOnlyResponse = {
    ...NBA_STATS_QUERY_RESPONSE,
    summary: {
      ...NBA_STATS_QUERY_RESPONSE.summary,
      percentiles: {},
      metric_categories: {
        points: "basic",
        rebounds: "basic",
        assists: "basic",
      },
      metrics: {
        points: 28.5,
        rebounds: 4.0,
        assists: 7.2,
      },
    },
    metric_labels: {
      points: "Points",
      rebounds: "Rebounds",
      assists: "Assists",
    },
  };

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
    if (url.pathname === "/api/research/stats/query") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(basicOnlyResponse),
      });
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
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });

  await page.goto("/stats", { waitUntil: "domcontentloaded" });
  // Use a distinct query string so we don't share React-Query cache state with
  // the previous test in the same Playwright worker.
  await page.getByTestId("sa-input").fill("Jalen Brunson this season");
  await page.getByTestId("sa-run").click();

  await expect(page.getByTestId("sa-answer")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId("sa-metric-points")).toBeVisible();
  // Advanced grid should NOT render when categories has no "advanced" entries.
  await expect(page.getByTestId("sa-advanced-grid")).toHaveCount(0);
});
