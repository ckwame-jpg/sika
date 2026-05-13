import { fileURLToPath } from "node:url";
import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const rootDir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": rootDir,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./test/setup.ts"],
    // Bug #24: ``lib/`` houses non-component utilities (SWR key
    // builders, format helpers). Include their ``.test.ts`` siblings
    // so vitest picks them up alongside the component suite.
    include: ["./components/**/*.test.tsx", "./lib/**/*.test.ts"],
    restoreMocks: true,
    clearMocks: true,
    environmentOptions: {
      jsdom: {
        url: "http://127.0.0.1/",
      },
    },
  },
});
