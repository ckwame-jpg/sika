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
    if (url.pathname === "/api/product/freshness") {
      // Shell-level freshness banner — not part of /trade regression scope.
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ overall_status: "fresh", scopes: [] }),
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

  // KPI quad — new Phase 1 testids + values
  await expect(page.getByTestId("trade-kpi-events")).toHaveText("1");
  await expect(page.getByTestId("trade-kpi-candidate-markets")).toHaveText("7");
  await expect(page.getByTestId("trade-kpi-recommendations")).toHaveText("7");
  await expect(page.getByTestId("trade-kpi-avg-edge")).toHaveText("+11.2%");

  // Hero chips
  await expect(page.getByTestId("trade-hero-chip-avg-edge")).toContainText("+11.2%");
  await expect(page.getByTestId("trade-hero-chip-top-quartile")).toContainText("+10.0%");

  await expect(page.getByText("Your Exposure")).toHaveCount(0);
  await expect(page.getByText("Event Context")).toHaveCount(0);

  await page.getByRole("button", { name: /Toronto Raptors to win/i }).click();
  await expect(ticketTitle).toHaveText("Toronto Raptors to win");

  const propCard = page.getByTestId("trade-prop-card").first();
  await propCard.getByRole("button", { name: "4+" }).click();
  await expect(propCard.getByTestId("trade-prop-summary-label")).toHaveText("4+ assists");
  await expect(propCard.getByTestId("trade-prop-summary-win-prob")).toHaveText("89.4%");
  await expect(propCard.getByTestId("trade-prop-summary-edge")).toHaveText("+4.4%");
  await expect(ticketTitle).toHaveText("Davion Mitchell 4+ assists");

  await propCard.getByRole("button", { name: "10+" }).click();
  await expect(propCard.getByTestId("trade-prop-summary-label")).toHaveText("10+ points");
  await expect(propCard.getByTestId("trade-prop-summary-win-prob")).toHaveText("72.1%");
  await expect(propCard.getByTestId("trade-prop-summary-edge")).toHaveText("+32.1%");
  await expect(ticketTitle).toHaveText("Davion Mitchell 10+ points");
  await expect(propCard.locator('[data-testid="trade-threshold-chip"][aria-pressed="true"]')).toHaveCount(1);

  expect(positionsRequested).toBe(false);
  expect(unexpectedApiRequests).toEqual([]);
});
