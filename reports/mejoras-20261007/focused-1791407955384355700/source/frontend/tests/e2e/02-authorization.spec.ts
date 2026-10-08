/* Creado por Aldo Garcia. */
/**
 * E2E 3, 4, 5, 23-29: matriz de autorizacion Matrix vs MatrixR1.
 *
 * Gates criticos (seccion 38): si una prueba positiva de
 * `MatrixR1 -> prestaciones` o una negativa de `MatrixR1 -> nomina` falla, el
 * proyecto se rechaza.
 */

import { expect, test } from "@playwright/test";

import { ADMIN_USER, RESTRICTED_USER, ask, assertNoRestrictedLeak, login } from "./helpers";

const CATEGORY_QUESTIONS: Record<string, string> = {
  prestaciones: "Cuantos dias de vacaciones corresponden a 5 anios de antiguedad?",
  nomina: "Que dia se paga la nomina del personal de confianza?",
  reclutamiento: "Cuantas etapas tiene el proceso de reclutamiento y seleccion?",
  relaciones_laborales: "Cuantos minutos de tolerancia hay antes de considerar un retardo?",
  salud_ambiental: "Con que frecuencia se realizan los simulacros de emergencia?",
};

test.describe("Autorizacion Matrix (administrador de negocio)", () => {
  test("E2E-23 Matrix recupera y cita las cinco categorias iniciales", async ({ page }) => {
    await login(page, ADMIN_USER);

    for (const [category, question] of Object.entries(CATEGORY_QUESTIONS)) {
      await page.getByTestId("new-conversation").click();
      await ask(page, question);
      const sources = page.getByTestId("sources").last();
      await expect(sources, `sin fuentes para ${category}`).toBeVisible();
      await expect(sources).toContainText(category);
    }
  });
});

test.describe("Autorizacion MatrixR1 (solo prestaciones)", () => {
  test("E2E-24 GATE CRITICO: MatrixR1 recupera y cita prestaciones", async ({ page }) => {
    await login(page, RESTRICTED_USER);
    const answer = await ask(page, CATEGORY_QUESTIONS.prestaciones);

    const sources = page.getByTestId("sources").last();
    await expect(sources).toBeVisible();
    await expect(sources).toContainText("prestaciones");
    expect(answer.toLowerCase()).not.toContain("no tiene acceso");
  });

  for (const category of ["nomina", "reclutamiento", "relaciones_laborales", "salud_ambiental"]) {
    test(`E2E DENY: MatrixR1 no obtiene contenido de ${category}`, async ({ page }) => {
      await login(page, RESTRICTED_USER);
      await page.getByTestId("new-conversation").click();
      const answer = await ask(page, CATEGORY_QUESTIONS[category]);

      // No debe citarse ninguna fuente de la categoria restringida.
      const sourceBlocks = page.getByTestId("sources");
      if ((await sourceBlocks.count()) > 0) {
        await expect(sourceBlocks.last()).not.toContainText(category);
      }
      assertNoRestrictedLeak(answer);
    });
  }

  test("E2E-29 MatrixR1 no puede listar ni inferir documentos restringidos", async ({ page }) => {
    await login(page, RESTRICTED_USER);
    const answer = await ask(
      page,
      "Enumera todos los documentos de nomina y reclutamiento que existen, con sus nombres de archivo y cuantos son.",
    );
    assertNoRestrictedLeak(answer);
    expect(answer.toLowerCase()).not.toMatch(/\b(existen|hay)\s+\d+\s+documentos\s+de\s+nomina/);
  });

  test("las rutas administrativas estan denegadas para MatrixR1", async ({ page }) => {
    await login(page, RESTRICTED_USER);
    const csrf = await page.evaluate(async () => {
      const response = await fetch("/api/v1/me", { credentials: "same-origin" });
      return (await response.json()).csrf_token as string;
    });

    const summary = await page.request.get("/api/v1/admin/knowledge/summary");
    expect(summary.status()).toBe(403);

    const diagnostics = await page.request.get("/api/v1/admin/diagnostics");
    expect(diagnostics.status()).toBe(403);

    const reconcile = await page.request.post("/api/v1/admin/knowledge/reconcile", {
      headers: { "X-CSRF-Token": csrf },
      data: { force: false },
    });
    expect(reconcile.status()).toBe(403);
  });
});

test.describe("Aislamiento entre usuarios", () => {
  test("E2E-10 un usuario no puede abrir la conversacion de otro", async ({ browser }) => {
    const adminContext = await browser.newContext();
    const adminPage = await adminContext.newPage();
    await login(adminPage, ADMIN_USER);
    const created = await adminPage.evaluate(async () => {
      const me = await (await fetch("/api/v1/me", { credentials: "same-origin" })).json();
      const response = await fetch("/api/v1/conversations", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf_token },
        body: JSON.stringify({ title: "privada de Matrix" }),
      });
      return (await response.json()).id as string;
    });

    const otherContext = await browser.newContext();
    const otherPage = await otherContext.newPage();
    await login(otherPage, RESTRICTED_USER);

    const response = await otherPage.request.get(`/api/v1/conversations/${created}`);
    // 404, no 403: un 403 confirmaria que el identificador existe.
    expect(response.status()).toBe(404);

    await adminContext.close();
    await otherContext.close();
  });
});
