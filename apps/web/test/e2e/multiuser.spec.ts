import { expect, test } from "@playwright/test";

/**
 * Multi-user batch PR 6 — end-to-end happy path covering the full
 * operator flow:
 *
 *   1. Visit /settings/users → see the empty state
 *   2. Add "canaan" via the form → row appears
 *   3. Click the topbar dropdown → see canaan in the list → click
 *   4. /me reflects the switch (covered via the topbar label)
 *   5. Navigate to /settings/kalshi → not-configured status
 *   6. Save Kalshi credentials → "Connected as <key_id>" appears
 *   7. Disconnect → reverts to not-configured
 *
 * All backend traffic is intercepted via ``page.route`` so the test
 * doesn't depend on a live API. Per-endpoint route handlers below
 * model the multi-user-batch backend behavior (PRs 1+5).
 */

const SAMPLE_KEY_ID = "12345678-aaaa-bbbb-cccc-deadbeef0000";
const SAMPLE_PEM = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n";

test("operator adds canaan, switches to him, and connects Kalshi", async ({ page }) => {
  // Start with no user cookie + an empty users table.
  await page.addInitScript(() => {
    document.cookie = "sika.userId=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
  });

  let users: { id: number; username: string; display_name: string; is_kalshi_owner: boolean }[] = [];
  let currentUsername = "";
  let kalshiCreds: {
    configured: boolean;
    key_id: string | null;
    base_url: string | null;
    updated_at: string | null;
  } = {
    configured: false,
    key_id: null,
    base_url: null,
    updated_at: null,
  };
  let nextUserId = 1;

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    const path = url.pathname;

    if (path === "/api/me") {
      const user = users.find((u) => u.username === currentUsername);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ user: user ?? null }),
      });
      return;
    }
    if (path === "/api/users" && method === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(users),
      });
      return;
    }
    if (path === "/api/users" && method === "POST") {
      const payload = JSON.parse(route.request().postData() || "{}");
      const username = (payload.username || "").toLowerCase().trim();
      const existing = users.find((u) => u.username === username);
      const row =
        existing ??
        {
          id: nextUserId++,
          username,
          display_name: payload.display_name || username,
          is_kalshi_owner: false,
        };
      if (!existing) users.push(row);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(row),
      });
      return;
    }
    if (path === "/api/users/switch") {
      const payload = JSON.parse(route.request().postData() || "{}");
      currentUsername = payload.username;
      const user = users.find((u) => u.username === currentUsername) ?? null;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ user }),
      });
      return;
    }
    if (path === "/api/users/sign-out") {
      currentUsername = "";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ user: null }),
      });
      return;
    }
    if (path === "/api/me/kalshi-credentials" && method === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(kalshiCreds),
      });
      return;
    }
    if (path === "/api/me/kalshi-credentials" && method === "POST") {
      const payload = JSON.parse(route.request().postData() || "{}");
      kalshiCreds = {
        configured: true,
        key_id: payload.key_id,
        base_url: payload.base_url,
        updated_at: new Date().toISOString(),
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(kalshiCreds),
      });
      return;
    }
    if (path === "/api/me/kalshi-credentials" && method === "DELETE") {
      kalshiCreds = { configured: false, key_id: null, base_url: null, updated_at: null };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(kalshiCreds),
      });
      return;
    }
    if (path === "/api/health" || path === "/api/product/freshness") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(path === "/api/health" ? {
          status: "ok", environment: "test", scheduler_enabled: false,
          refresh_status: "idle", refresh_reason: "none",
          last_successful_refresh_at: null, data_stale: false,
          refresh_error_message: null, prop_refresh_status: "idle",
          prop_refresh_reason: "none", last_prop_refresh_at: null,
          prop_data_stale: false, prop_refresh_error_message: null,
          active_refresh_job: null, latest_refresh_job: null,
          active_prop_refresh_job: null, latest_prop_refresh_job: null,
          active_settlement_job: null, latest_settlement_job: null,
          upstream_sources: [],
        } : { overall_status: "fresh", scopes: [] }),
      });
      return;
    }
    // Fall through — unmocked endpoint → 404 so unexpected calls fail loudly.
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Not found in mock" }),
    });
  });

  // 1. Land on /settings/users — empty state.
  await page.goto("/settings/users");
  await expect(page.getByTestId("settings-users-list")).toContainText(/no users yet/i);

  // 2. Add canaan.
  await page.getByTestId("settings-users-username").fill("canaan");
  await page.getByTestId("settings-users-display-name").fill("Canaan");
  await page.getByTestId("settings-users-add-submit").click();
  await expect(page.getByTestId("settings-users-row-canaan")).toBeVisible();

  // 3. Topbar dropdown shows canaan; click to switch.
  await page.getByTestId("user-switcher-trigger").click();
  await page.getByTestId("user-switcher-item-canaan").click();

  // 4. Topbar label updates to Canaan.
  await expect(page.getByTestId("user-switcher-trigger")).toContainText(/Canaan/);

  // 5. Navigate to /settings/kalshi — not configured yet.
  await page.goto("/settings/kalshi");
  await expect(page.getByTestId("settings-kalshi-status")).toContainText(/not connected/i);

  // 6. Fill in credentials and save.
  await page.getByTestId("settings-kalshi-key-id").fill(SAMPLE_KEY_ID);
  await page.getByTestId("settings-kalshi-pem").fill(SAMPLE_PEM);
  await page.getByTestId("settings-kalshi-save").click();
  await expect(page.getByTestId("settings-kalshi-status")).toContainText(SAMPLE_KEY_ID);
  await expect(page.getByTestId("settings-kalshi-saved")).toBeVisible();

  // 7. Disconnect and verify it reverts.
  await page.getByTestId("settings-kalshi-disconnect").click();
  await expect(page.getByTestId("settings-kalshi-status")).toContainText(/not connected/i);
});
