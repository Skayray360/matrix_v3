/* Creado por Aldo Garcia. */
/** Navegador real con API sintetica: no certifica el IdP ni la inferencia. */
import { test, expect, type Page } from "@playwright/test";

const profile = {
  user_id: "ui",
  username: "usuario-ui",
  display_name: "MatrixR1",
  auth_source: "local_test",
  roles: ["prestaciones_reader_test"],
  permissions: [],
  allowed_categories: ["prestaciones"],
  category_wildcard: false,
  csrf_token: "csrf-sintetico-ui",
};
const conversation = {
  id: "conversacion-ui",
  title: "Prueba de interfaz",
  created_at: "2026-09-30T12:00:00Z",
  updated_at: "2026-09-30T12:00:00Z",
};
const attachment = {
  id: "adjunto-ui",
  filename: "manual.txt",
  status: "indexed",
  chunk_count: 1,
  scope: "conversation",
  category: null,
  error_message: null,
};
const reply = {
  conversation_id: conversation.id,
  message_id: "respuesta-ui",
  answer: "Esta es una respuesta sintética para validar la interfaz. [fuente-ui]",
  sources: [
    {
      source_id: "fuente-ui",
      category: "prestaciones",
      filename: "manual-ui.txt",
      section: "Ejemplo visual",
      page_or_sheet: "",
      score: 0.9,
      label: "Manual de prueba",
      scope: "corporate",
    },
  ],
  intent: "documental",
  grounded: true,
  latency_ms: 240,
};

async function mockApi(page: Page, queued = false, general = false) {
  const response = general ? { ...reply, intent: "general", grounded: false, sources: [] } : reply;
  let authenticated = false;
  let completed = false;
  let cancelled = false;
  let attached = false;
  const errors: string[] = [];
  const unexpected: string[] = [];
  const mutations: { path: string; csrf: string | undefined; body: string | null }[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (new URL(request.url()).hostname !== "127.0.0.1") unexpected.push(request.url());
  });
  const status = () => ({
    status: cancelled ? "cancelled" : completed ? "completed" : "queued",
    conversation_id: conversation.id,
    response: completed ? response : null,
    capacity: {
      active: completed ? 1 : 9,
      limit: 10,
      queued: completed ? 0 : 1,
      queue_limit: 50,
      utilization_pct: completed ? 10 : 90,
      overloaded: !completed,
    },
    notice: null,
    poll_after_ms: 2000,
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    const method = request.method();
    if (method !== "GET")
      mutations.push({ path, csrf: request.headers()["x-csrf-token"], body: request.postData() });
    const send = (json: unknown, code = 200) =>
      route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(json) });
    if (path === "/me")
      return authenticated
        ? send(profile)
        : send({ code: "unauthenticated", message: "Inicie sesión" }, 401);
    if (path === "/auth/local/login") {
      authenticated = true;
      return send(profile);
    }
    if (path === "/auth/logout") {
      authenticated = false;
      return send({ ok: true });
    }
    if (path === "/conversations") return send(method === "POST" ? conversation : [conversation]);
    if (path === `/conversations/${conversation.id}/attachments`) {
      attached = true;
      return send({ documents: [attachment] });
    }
    if (path === `/conversations/${conversation.id}`)
      return send({
        ...conversation,
        attachments: attached ? [attachment] : [],
        messages: completed
          ? [
              {
                id: response.message_id,
                role: "assistant",
                content: response.answer,
                intent: response.intent,
                model: null,
                created_at: conversation.created_at,
                sources: response.sources,
              },
            ]
          : [],
      });
    if (path === "/chat/submit") {
      completed = !queued;
      return send(status(), 202);
    }
    if (/^\/chat\/requests\/[^/]+\/cancel$/.test(path)) {
      cancelled = true;
      return send({ ok: true });
    }
    if (/^\/chat\/requests\/[^/]+$/.test(path)) return send(status());
    unexpected.push(`${method} ${path}`);
    return send({ message: "Ruta no prevista por la prueba" }, 501);
  });
  return {
    complete: () => {
      completed = true;
    },
    errors,
    unexpected,
    mutations,
  };
}

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("Usuario", { exact: true }).fill("usuario-ui");
  await page.getByLabel("Contrasena", { exact: true }).fill("dato-sintetico");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(page.getByTestId("identity-user")).toHaveText("MatrixR1");
}

async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(
    true,
  );
  await expect
    .poll(() =>
      page.evaluate(() =>
        Array.from(document.images).every((img) => img.complete && img.naturalWidth > 0),
      ),
    )
    .toBe(true);
  expect(
    await page.locator(".vite-error-overlay, #webpack-dev-server-client-overlay").count(),
  ).toBe(0);
}

test("recuperación de un fallo de render mediante recarga", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 667 });
  let profileReads = 0;
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = path.endsWith("/me")
      ? { ...profile, allowed_categories: ++profileReads === 1 ? null : profile.allowed_categories }
      : [];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(json),
    });
  });
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("No se pudo mostrar Matrix RH");
  await expect(page.locator(".recovery-card")).not.toContainText("TypeError");
  await noOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("recovery-mobile.png"), fullPage: true });
  await page.getByRole("button", { name: "Recargar Matrix RH" }).click();
  await expect(
    page.getByRole("heading", { name: "Consulta conocimiento interno con contexto" }),
  ).toBeVisible();
  expect(profileReads).toBe(2);
  expect(errors).toEqual([]);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
  { width: 320, height: 667 },
]) {
  test(`login y chat sin recortes a ${viewport.width}x${viewport.height}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    const api = await mockApi(page);
    await page.goto("/");
    await expect(
      page.getByRole("link", { name: "Continuar con acceso corporativo" }),
    ).toHaveAttribute("href", "/api/v1/auth/login");
    await expect(page.locator(".login-shell")).toHaveCSS("background-image", /login-bg/);
    await noOverflow(page);
    await page.screenshot({
      path: testInfo.outputPath(`login-${viewport.width}.png`),
      fullPage: true,
    });
    await login(page);
    await expect(
      page.getByRole("heading", { name: "Consulta conocimiento interno con contexto" }),
    ).toBeVisible();
    await expect(page.getByRole("img", { name: "Asistente IA Matrix" })).toBeVisible();
    expect(
      await page.locator(".welcome-mascot").evaluate((img) => {
        const a = img.getBoundingClientRect();
        const b = img.closest(".messages")!.getBoundingClientRect();
        return a.top >= b.top && a.bottom <= b.bottom && a.left >= b.left && a.right <= b.right;
      }),
    ).toBe(true);
    await expect(page.getByTestId("identity-scope")).toHaveText("1 categoria(s)");
    await noOverflow(page);
    await page.screenshot({
      path: testInfo.outputPath(`chat-${viewport.width}.png`),
      fullPage: true,
    });
    expect(api.errors).toEqual([]);
    expect(api.unexpected).toEqual([]);
  });
}

test("consulta, fuentes, trazabilidad y cierre de sesión con CSRF", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const api = await mockApi(page);
  await login(page);
  await page.getByTestId("composer-input").fill("Consulta sintética de prestaciones");
  await page.getByTestId("send-button").click();
  await expect(page.getByTestId("message-assistant")).toContainText("respuesta sintética");
  await expect(page.getByTestId("sources")).toContainText("Manual de prueba");
  await expect(page.getByTestId("capacity-notice")).toHaveCount(0);
  await page.getByRole("button", { name: "Mostrar u ocultar trazabilidad" }).click();
  await expect(page.locator(".inspector")).toBeVisible();
  await noOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("chat-response.png"), fullPage: true });
  await page.getByRole("button", { name: "Salir", exact: true }).click();
  await expect(page.getByRole("button", { name: "Entrar", exact: true })).toBeVisible();
  expect(
    api.mutations
      .filter((m) => m.path !== "/auth/local/login")
      .every((m) => m.csrf === profile.csrf_token),
  ).toBe(true);
  expect(api.errors).toEqual([]);
  expect(api.unexpected).toEqual([]);
});

test("aviso general visible y persistente en móvil", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 667 });
  const api = await mockApi(page, false, true);
  await login(page);
  await page.getByTestId("composer-input").fill("Pregunta de conocimiento general");
  await page.getByTestId("send-button").click();
  await expect(page.getByTestId("general-notice")).toHaveText(
    "Respuesta general: no basada en documentación de la empresa",
  );
  await expect(page.getByTestId("sources")).toHaveCount(0);
  await noOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("general-mobile.png"), fullPage: true });
  await page.getByRole("button", { name: "Conversaciones", exact: true }).click();
  await page.getByRole("button", { name: /^Prueba de interfaz/ }).click();
  await expect(page.getByTestId("general-notice")).toBeVisible();
  expect(api.errors).toEqual([]);
  expect(api.unexpected).toEqual([]);
});

test("aviso al 90 por ciento y recuperación de respuesta en móvil", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const api = await mockApi(page, true);
  await login(page);
  await page.getByTestId("composer-input").fill("Pregunta en cola");
  await page.getByTestId("send-button").click();
  await expect(page.getByTestId("capacity-notice")).toBeVisible();
  await expect(page.getByTestId("send-button")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Cancelar solicitud" })).toBeVisible();
  await noOverflow(page);
  api.complete();
  await expect(page.getByTestId("message-assistant")).toContainText("respuesta sintética");
  await expect(page.getByTestId("capacity-notice")).toHaveCount(0);
  expect(api.errors).toEqual([]);
  expect(api.unexpected).toEqual([]);
});

test("cancelación conserva el borrador", async ({ page }) => {
  const api = await mockApi(page, true);
  await login(page);
  await page.getByTestId("composer-input").fill("Borrador que se conserva");
  await page.getByTestId("send-button").click();
  await page.getByRole("button", { name: "Cancelar solicitud" }).click();
  await expect(page.getByTestId("error-banner")).toContainText("Solicitud cancelada");
  await expect(page.getByTestId("composer-input")).toHaveValue("Borrador que se conserva");
  expect(api.mutations.find((m) => m.path.endsWith("/cancel"))?.csrf).toBe(profile.csrf_token);
  expect(api.errors).toEqual([]);
  expect(api.unexpected).toEqual([]);
});

test("menú móvil, tema oscuro y adjunto privado", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const api = await mockApi(page);
  await login(page);
  await page.getByRole("button", { name: "Conversaciones", exact: true }).click();
  await expect(page.locator(".sidebar")).toHaveAttribute("data-open", "true");
  await page.locator(".sidebar-close").click();
  await expect(page.locator(".sidebar")).toHaveAttribute("data-open", "false");
  await page.getByTestId("theme-toggle").click();
  await page.getByTestId("theme-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByLabel("Adjuntar archivos a la conversacion", { exact: true }).setInputFiles({
    name: "manual.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Documento sintético para la prueba visual."),
  });
  await expect(page.getByTestId("attachments")).toContainText("manual.txt");
  await expect(page.getByTestId("attachments")).toContainText("indexed");
  await noOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("chat-dark-mobile.png"), fullPage: true });
  expect(
    api.mutations
      .filter((m) => m.path !== "/auth/local/login")
      .every((m) => m.csrf === profile.csrf_token),
  ).toBe(true);
  expect(api.errors).toEqual([]);
  expect(api.unexpected).toEqual([]);
});
