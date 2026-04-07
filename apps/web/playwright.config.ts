import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

const chromeExecutable =
  process.env.PLAYWRIGHT_CHROME_PATH ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const rootDir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  testDir: "./test/e2e",
  fullyParallel: false,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3107",
    headless: true,
    browserName: "chromium",
    launchOptions: fs.existsSync(chromeExecutable)
      ? { executablePath: chromeExecutable }
      : undefined,
  },
  webServer: {
    command: "npm run dev -- --hostname 127.0.0.1 --port 3107",
    cwd: rootDir,
    env: {
      ...process.env,
      SIKA_API_BASE_URL: "http://127.0.0.1:8999",
    },
    url: "http://127.0.0.1:3107/trade",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
