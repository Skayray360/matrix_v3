/* Creado por Aldo Garcia. */
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/visual",
  timeout: 30_000,
  expect: { timeout: 8_000 },
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"], ["json", { outputFile: "../reports/tests/visual-results.json" }]],
  webServer: {
    command: "node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 5174 --strictPort",
    url: "http://127.0.0.1:5174",
    reuseExistingServer: false,
    timeout: 20_000,
  },
  use: {
    baseURL: "http://127.0.0.1:5174",
    browserName: "chromium",
    locale: "es-MX",
    colorScheme: "light",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    launchOptions: process.env.MATRIX_VISUAL_CHROMIUM
      ? { executablePath: process.env.MATRIX_VISUAL_CHROMIUM }
      : undefined,
  },
});
