/* Creado por Aldo Garcia. */
/**
 * E2E 1, 2, 20, 21, 22, 30, 33: autenticacion y sesion.
 */

import { expect, test } from "@playwright/test";

import { ADMIN_USER, RESTRICTED_USER, TEST_PASSWORD, login, logout } from "./helpers";

test.describe("Autenticacion", () => {
  test("E2E-21 login Matrix funciona en modo local de test", async ({ page }) => {
    await login(page, ADMIN_USER);
    await expect(page.getByTestId("identity-source")).toHaveText("local_test");
    await expect(page.getByTestId("identity-scope")).toContainText("ampliado");
  });

  test("E2E-22 login MatrixR1 funciona en modo local de test", async ({ page }) => {
    await login(page, RESTRICTED_USER);
    await expect(page.getByTestId("identity-user")).toContainText("MatrixR1");
    await expect(page.getByTestId("identity-scope")).toContainText("1 categoria");
  });

  test("E2E-02/33 login rechazado no revela si el usuario existe", async ({ page }) => {
    await page.goto("/");

    // Usuario inexistente
    await page.getByLabel("Usuario").fill("UsuarioQueNoExiste");
    await page.getByLabel("Contrasena").fill("cualquier-cosa");
    await page.getByRole("button", { name: "Entrar" }).click();
    const unknownUserMessage = await page.getByTestId("login-error").innerText();

    // Usuario existente con contrasena incorrecta
    await page.getByLabel("Usuario").fill(ADMIN_USER);
    await page.getByLabel("Contrasena").fill("contrasena-incorrecta");
    await page.getByRole("button", { name: "Entrar" }).click();
    const badPasswordMessage = await page.getByTestId("login-error").innerText();

    expect(badPasswordMessage).toBe(unknownUserMessage);
    expect(badPasswordMessage.toLowerCase()).not.toContain("no existe");
    await expect(page.getByTestId("composer-input")).toHaveCount(0);
  });

  test("E2E-20 logout invalida la sesion", async ({ page }) => {
    await login(page, ADMIN_USER);
    await logout(page);

    // Tras cerrar sesion, /me debe responder 401 y la app volver al acceso.
    const response = await page.request.get("/api/v1/me");
    expect(response.status()).toBe(401);

    await page.reload();
    await expect(page.getByRole("button", { name: "Entrar" })).toBeVisible();
  });

  test("E2E-30 manipular rol o user_id desde el cliente no eleva permisos", async ({ page }) => {
    await login(page, RESTRICTED_USER);

    // Intento de suplantacion por cuerpo y por cabeceras: el backend solo mira
    // la cookie de sesion y sus propias politicas.
    const csrf = await page.evaluate(async () => {
      const response = await fetch("/api/v1/me", { credentials: "same-origin" });
      const body = await response.json();
      return body.csrf_token as string;
    });

    const forged = await page.request.post("/api/v1/chat", {
      headers: {
        "X-CSRF-Token": csrf,
        "X-Roles": "matrix_admin_test",
        "X-User-Id": "00000000-0000-0000-0000-000000000000",
      },
      data: {
        message: "Que dia se paga la nomina del personal de confianza?",
        conversation_id: null,
        // Campos inventados: el esquema los rechaza (extra='forbid').
      },
    });
    expect(forged.status()).toBe(200);
    const payload = await forged.json();
    const sources = payload.sources as { category: string }[];
    expect(sources.every((source) => source.category !== "nomina")).toBe(true);

    const me = await (await page.request.get("/api/v1/me")).json();
    expect(me.roles).toEqual(["prestaciones_reader_test"]);
    expect(me.allowed_categories).toEqual(["prestaciones"]);
  });

  test("las peticiones mutantes sin token CSRF se rechazan", async ({ page }) => {
    await login(page, ADMIN_USER);
    const response = await page.request.post("/api/v1/conversations", {
      data: { title: "sin csrf" },
    });
    expect(response.status()).toBe(403);
  });

  test("la cookie de sesion es HttpOnly y no hay tokens en localStorage", async ({
    page,
    context,
  }) => {
    await login(page, ADMIN_USER, TEST_PASSWORD);

    const cookies = await context.cookies();
    const session = cookies.find((cookie) => cookie.name === "matrixrh_session");
    expect(session, "no se encontro la cookie de sesion").toBeTruthy();
    expect(session?.httpOnly).toBe(true);

    const storage = await page.evaluate(() => ({
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
    }));
    const serialized = JSON.stringify(storage).toLowerCase();
    expect(serialized).not.toContain("token");
    expect(serialized).not.toContain("matrixrh_session");
  });
});
