const { chromium } = require("playwright");
const fs = require("node:fs");
const { mkdir, writeFile } = require("node:fs/promises");
const path = require("node:path");

const baseUrl = process.env.AUDIT_BASE_URL ?? "http://127.0.0.1:3007";
const outputDir = path.resolve(process.cwd(), "output/click-audit");
const chromeExecutable =
  process.env.PLAYWRIGHT_CHROME_PATH ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

async function createPage(context, consoleErrors) {
  const page = await context.newPage();
  page.on("pageerror", (error) => {
    consoleErrors.push({ type: "pageerror", text: error.message });
  });
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push({ type: "console", text: message.text() });
    }
  });
  return page;
}

async function pause(page, ms = 1200) {
  await page.waitForTimeout(ms);
}

async function waitForTradeStable(page, timeout = 30000) {
  await page.waitForFunction(
    () => {
      const text = (document.body.innerText || "").toLowerCase();
      return (
        text.includes("open positions") ||
        text.includes("no live trade-ready markets") ||
        text.includes("research only") ||
        text.includes("trade desk failed to load") ||
        text.includes("api unavailable")
      );
    },
    undefined,
    { timeout },
  );
}

async function firstHeading(page) {
  const headings = page.locator("main h1, main h2, main h3");
  const count = await headings.count();
  if (count === 0) {
    return null;
  }
  const text = await headings.first().textContent();
  return (text || "").replace(/\s+/g, " ").trim() || null;
}

async function safeText(locator) {
  const count = await locator.count();
  if (count === 0) {
    return null;
  }
  const text = await locator.first().textContent();
  return (text || "").replace(/\s+/g, " ").trim() || null;
}

async function isVisible(locator) {
  try {
    return await locator.first().isVisible();
  } catch {
    return false;
  }
}

async function desktopAudit(browser) {
  const consoleErrors = [];
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
    deviceScaleFactor: 1,
  });
  const page = await createPage(context, consoleErrors);
  const report = {
    viewport: "desktop",
    nav: {},
    redirects: {},
    trade: {},
    consoleErrors,
    failures: [],
  };

  await page.goto(`${baseUrl}/trade`, { waitUntil: "domcontentloaded", timeout: 30000 });
  await waitForTradeStable(page);
  await pause(page, 500);

  report.trade.heading = await firstHeading(page);
  report.trade.apiUnavailable = await isVisible(page.getByText("API unavailable"));
  report.trade.loadFailure = await isVisible(page.getByText("Trade desk failed to load."));
  report.trade.openPositionsVisible = await isVisible(page.getByText(/open positions/i));
  if (report.trade.apiUnavailable || report.trade.loadFailure) {
    report.failures.push("Trade page did not load usable data.");
  }

  const navTargets = [
    { label: "Trade", path: "/trade" },
    { label: "Stats", path: "/stats" },
    { label: "Predictions", path: "/predictions" },
    { label: "Portfolio", path: "/positions" },
    { label: "Runs", path: "/runs" },
    { label: "Settings", path: "/settings" },
  ];

  for (const target of navTargets) {
    await page.getByRole("link", { name: new RegExp(`^${target.label}$`, "i") }).first().click();
    await page.waitForURL(`**${target.path}`, { timeout: 15000 });
    await pause(page, 900);
    report.nav[target.label] = {
      pathname: new URL(page.url()).pathname,
      heading: await firstHeading(page),
    };
  }

  for (const redirectPath of ["/watchlist?sport=NBA", "/markets?sport=NBA"]) {
    await page.goto(`${baseUrl}${redirectPath}`, { waitUntil: "domcontentloaded", timeout: 30000 });
    await pause(page, 1200);
    report.redirects[redirectPath] = new URL(page.url()).pathname + new URL(page.url()).search;
  }

  await page.goto(`${baseUrl}/trade`, { waitUntil: "domcontentloaded", timeout: 30000 });
  await waitForTradeStable(page);
  await pause(page, 500);

  const gameLinesTab = page.getByRole("button", { name: /^Game Lines$/i });
  if (await isVisible(gameLinesTab)) {
    await gameLinesTab.click();
    await pause(page, 500);
  }
  const gameLineRows = page.locator("button").filter({ hasText: /Model leans/i });
  report.trade.gameLineRowCount = await gameLineRows.count();
  if (report.trade.gameLineRowCount > 0) {
    report.trade.firstGameLineLabel = await safeText(gameLineRows.first());
    await gameLineRows.first().click();
    await pause(page, 700);
    report.trade.ticketVisibleAfterGameLineClick = await isVisible(page.getByText("Your Exposure"));
    report.trade.paperTradeVisible = await isVisible(page.getByRole("button", { name: /^Paper trade$/i }));
    if (!report.trade.ticketVisibleAfterGameLineClick) {
      report.failures.push("Clicking a game line did not open the trade ticket.");
    }

    const paperTradeButton = page.getByRole("button", { name: /^Paper trade$/i });
    if (await isVisible(paperTradeButton)) {
      await paperTradeButton.click();
      await pause(page, 500);
      report.trade.paperDialogVisible = await isVisible(page.getByRole("button", { name: /Open Paper Trade/i }));
      await page.getByRole("button", { name: /^Cancel$/i }).click();
      await pause(page, 300);
      if (!report.trade.paperDialogVisible) {
        report.failures.push("Paper trade action did not open the trade dialog.");
      }
    }
  }

  const playerPropsTab = page.getByRole("button", { name: /^Player Props$/i });
  if (await isVisible(playerPropsTab)) {
    await playerPropsTab.click();
    await pause(page, 500);
  }
  const propThresholdButtons = page.locator("button").filter({ hasText: /\d+\+/ });
  report.trade.playerPropThresholdCount = await propThresholdButtons.count();
  if (report.trade.playerPropThresholdCount > 0) {
    report.trade.firstPropThreshold = await safeText(propThresholdButtons.first());
    await propThresholdButtons.first().click();
    await pause(page, 700);
    report.trade.ticketVisibleAfterPropClick = await isVisible(page.getByText("Your Exposure"));
    if (!report.trade.ticketVisibleAfterPropClick) {
      report.failures.push("Clicking a player prop threshold did not open the trade ticket.");
    }
  }

  await page.screenshot({
    path: path.join(outputDir, "desktop-trade.png"),
    fullPage: true,
  });

  await context.close();
  return report;
}

async function mobileAudit(browser) {
  const consoleErrors = [];
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    deviceScaleFactor: 2,
  });
  const page = await createPage(context, consoleErrors);
  const report = {
    viewport: "mobile",
    trade: {},
    consoleErrors,
    failures: [],
  };

  await page.goto(`${baseUrl}/trade`, { waitUntil: "domcontentloaded", timeout: 30000 });
  await waitForTradeStable(page);
  await pause(page, 500);

  report.trade.apiUnavailable = await isVisible(page.getByText("API unavailable"));
  report.trade.loadFailure = await isVisible(page.getByText("Trade desk failed to load."));
  if (report.trade.apiUnavailable || report.trade.loadFailure) {
    report.failures.push("Mobile trade page did not load usable data.");
  }

  const firstGameLineRow = page.locator("button").filter({ hasText: /Model leans/i }).first();
  if (await firstGameLineRow.count()) {
    await firstGameLineRow.click();
    await pause(page, 700);
    report.trade.sheetVisibleAfterGameLineClick = await isVisible(page.getByRole("button", { name: /^Close$/i }));
    if (!report.trade.sheetVisibleAfterGameLineClick) {
      report.failures.push("Mobile game line click did not open the bottom sheet.");
    } else {
      await page.getByLabel("Close").click();
      await pause(page, 500);
    }
  }

  const firstPropThreshold = page.locator("button").filter({ hasText: /\d+\+/ }).first();
  if (await firstPropThreshold.count()) {
    await firstPropThreshold.click();
    await pause(page, 700);
    report.trade.sheetVisibleAfterPropClick = await isVisible(page.getByRole("button", { name: /^Close$/i }));
    if (!report.trade.sheetVisibleAfterPropClick) {
      report.failures.push("Mobile prop click did not open the bottom sheet.");
    } else {
      await page.getByLabel("Close").click();
      await pause(page, 500);
    }
  }

  await page.screenshot({
    path: path.join(outputDir, "mobile-trade.png"),
    fullPage: true,
  });

  await context.close();
  return report;
}

async function run() {
  await mkdir(outputDir, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: fs.existsSync(chromeExecutable) ? chromeExecutable : undefined,
  });

  const report = {
    baseUrl,
    desktop: await desktopAudit(browser),
    mobile: await mobileAudit(browser),
  };

  await browser.close();

  const reportPath = path.join(outputDir, "click-audit.json");
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");

  const failures = [...report.desktop.failures, ...report.mobile.failures];
  if (failures.length > 0) {
    console.error(JSON.stringify({ reportPath, failures }, null, 2));
    process.exit(1);
  }

  console.log(reportPath);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
