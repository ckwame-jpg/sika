const { chromium } = require("playwright");
const { mkdir, writeFile } = require("node:fs/promises");
const fs = require("node:fs");
const path = require("node:path");

const baseUrl = process.env.AUDIT_BASE_URL ?? "http://127.0.0.1:3005";
const outputDir = path.resolve(process.cwd(), "output/mobile-audit");
const chromeExecutable =
  process.env.PLAYWRIGHT_CHROME_PATH ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const pages = [
  { key: "dashboard", path: "/" },
  { key: "watchlist", path: "/watchlist?sport=NBA" },
  { key: "markets", path: "/markets?sport=NBA" },
  { key: "predictions", path: "/predictions" },
  { key: "events", path: "/events?sport=NBA" },
  { key: "runs", path: "/runs" },
  { key: "stats", path: "/stats" },
  { key: "settings", path: "/settings" },
  { key: "models", path: "/settings/models" },
  { key: "positions", path: "/positions" },
  { key: "demo", path: "/positions/demo" },
];

const viewports = [
  { key: "phone", width: 390, height: 844, isMobile: true },
  { key: "desktop", width: 1440, height: 1100, isMobile: false },
];

async function collectLayout(page) {
  return page.evaluate(() => {
    const root = document.scrollingElement ?? document.documentElement;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const elements = Array.from(document.querySelectorAll("body *"));
    const offenders = [];

    for (const element of elements) {
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      const invisible =
        style.display === "none" ||
        style.visibility === "hidden" ||
        parseFloat(style.opacity || "1") === 0;
      const trivial = rect.width < 2 || rect.height < 2;

      if (invisible || trivial) {
        continue;
      }

      const overflowRight = Math.max(0, rect.right - viewportWidth);
      const overflowLeft = Math.max(0, 0 - rect.left);
      const tooWide = rect.width - viewportWidth;

      if (overflowRight > 1 || overflowLeft > 1 || tooWide > 1) {
        const text = (element.textContent || "").replace(/\s+/g, " ").trim().slice(0, 120);
        offenders.push({
          tag: element.tagName.toLowerCase(),
          id: element.id || null,
          classes: element.className || null,
          text,
          rect: {
            left: Number(rect.left.toFixed(1)),
            right: Number(rect.right.toFixed(1)),
            width: Number(rect.width.toFixed(1)),
            top: Number(rect.top.toFixed(1)),
            bottom: Number(rect.bottom.toFixed(1)),
          },
          overflowRight: Number(overflowRight.toFixed(1)),
          overflowLeft: Number(overflowLeft.toFixed(1)),
          tooWide: Number(tooWide.toFixed(1)),
        });
      }
    }

    offenders.sort((a, b) => {
      const aMax = Math.max(a.overflowRight, a.overflowLeft, a.tooWide);
      const bMax = Math.max(b.overflowRight, b.overflowLeft, b.tooWide);
      return bMax - aMax;
    });

    return {
      location: window.location.pathname + window.location.search,
      title: document.title,
      viewportWidth,
      viewportHeight,
      scrollWidth: root.scrollWidth,
      clientWidth: root.clientWidth,
      horizontalOverflow: root.scrollWidth - viewportWidth,
      offenders: offenders.slice(0, 20),
    };
  });
}

async function run() {
  await mkdir(outputDir, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    executablePath: fs.existsSync(chromeExecutable) ? chromeExecutable : undefined,
  });
  const report = [];

  for (const viewport of viewports) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      isMobile: viewport.isMobile,
      deviceScaleFactor: 1,
    });

    for (const pageConfig of pages) {
      const page = await context.newPage();
      const url = `${baseUrl}${pageConfig.path}`;
      const consoleErrors = [];
      page.on("pageerror", (error) => {
        consoleErrors.push({ type: "pageerror", text: error.message });
      });
      page.on("console", (message) => {
        if (message.type() === "error") {
          consoleErrors.push({ type: "console", text: message.text() });
        }
      });

      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
      await page.waitForTimeout(2500);
      await page.screenshot({
        path: path.join(outputDir, `${pageConfig.key}-${viewport.key}.png`),
        fullPage: true,
      });
      const layout = await collectLayout(page);
      report.push({
        page: pageConfig.key,
        viewport: viewport.key,
        url,
        consoleErrors,
        ...layout,
      });
      await page.close();
    }

    await context.close();
  }

  const reportPath = path.join(outputDir, "layout-audit.json");
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  console.log(reportPath);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
