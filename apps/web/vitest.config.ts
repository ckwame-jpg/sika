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
    include: ["./components/**/*.test.tsx"],
    restoreMocks: true,
    clearMocks: true,
    environmentOptions: {
      jsdom: {
        url: "http://127.0.0.1/",
      },
    },
  },
});
