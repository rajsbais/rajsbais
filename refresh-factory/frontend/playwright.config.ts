import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";

// The system Chromium is used when present (no browser download needed); otherwise Playwright's own build is used.
const chromium = process.env.PW_CHROMIUM ?? (existsSync("/opt/pw-browsers/chromium") ? "/opt/pw-browsers/chromium" : undefined);

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  expect: { timeout: 8_000 },
  workers: 1, // every test starts its own backend, so tests are isolated and can run in any order
  retries: 0,
  reporter: [["list"]],
  use: {
    viewport: { width: 1400, height: 1000 },
    trace: "retain-on-failure",
    launchOptions: { executablePath: chromium, args: ["--no-sandbox"] },
  },
});
