/* Creado por Aldo Garcia. */
/**
 * Configuracion de Playwright.
 *
 * Los E2E se ejecutan contra el backend real sirviendo el build del frontend
 * same-origin (http://127.0.0.1:8000). Es la misma topologia que ve el usuario
 * al arrancar con INICIAR_MATRIX_RH.bat, de modo que la prueba valida el
 * despliegue real y no una maqueta.
 *
 * El servidor NO se levanta desde aqui: el flujo operativo lo arranca
 * VALIDAR_MATRIX_RH.bat antes de invocar Playwright. Asi la evidencia E2E
 * corresponde a la pila que realmente se entrega.
 */
import { defineConfig, devices } from "@playwright/test";

const BASE_URL = process.env.MATRIX_E2E_BASE_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  testDir: "./tests/e2e",
  // Los turnos de chat usan modelos locales: los tiempos son generosos a
  // proposito, no por inestabilidad de la interfaz.
  timeout: 240_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [
    ["list"],
    ["json", { outputFile: "../reports/tests/playwright-results.json" }],
    ["html", { outputFolder: "playwright-report", open: "never" }],
  ],
  use: {
    baseURL: BASE_URL,
    locale: "es-MX",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    ignoreHTTPSErrors: false,
  },
  projects: [{ name: "desktop-chromium", use: { ...devices["Desktop Chrome"] } }],
});
