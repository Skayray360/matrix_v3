/* Creado por Aldo Garcia. */
/** Utilidades compartidas por los E2E de Matrix RH. */

import { expect, type Page } from "@playwright/test";

/**
 * Credenciales sinteticas de prueba declaradas en la especificacion.
 * NO son secretos productivos: las cuentas se deshabilitan antes de produccion.
 */
export const TEST_PASSWORD = "Matrix RH";
export const ADMIN_USER = "Matrix";
export const RESTRICTED_USER = "MatrixR1";

export async function login(page: Page, username: string, password = TEST_PASSWORD): Promise<void> {
  await page.goto("/");
  await page.getByLabel("Usuario").fill(username);
  await page.getByLabel("Contrasena").fill(password);
  await page.getByRole("button", { name: "Entrar" }).click();
  await expect(page.getByTestId("identity-user")).toBeVisible({ timeout: 30_000 });
}

export async function ask(page: Page, question: string): Promise<string> {
  const answers = page.getByTestId("message-assistant");
  const previousCount = await answers.count();
  await page.getByTestId("composer-input").fill(question);
  // Captura el envio antes del click: una respuesta previa o un error que
  // oculte pending nunca debe contar como respuesta a esta pregunta.
  const [request] = await Promise.all([
    page.waitForRequest(
      (candidate) => {
        if (
          candidate.method() !== "POST" ||
          new URL(candidate.url()).pathname !== "/api/v1/chat/submit"
        )
          return false;
        try {
          return candidate.postDataJSON()?.message === question;
        } catch {
          return false;
        }
      },
      { timeout: 30_000 },
    ),
    page.getByTestId("send-button").click(),
  ]);
  const response = await request.response();
  if (!response || !response.ok()) {
    throw new Error(
      `El envio de chat no termino correctamente: HTTP ${response?.status() ?? "sin respuesta"}`,
    );
  }
  const sent = request.postDataJSON();
  if (typeof sent.client_request_id !== "string" || !sent.client_request_id) {
    throw new Error("El envio no tiene identificador para correlacionar su estado.");
  }
  let operation = await response.json();
  const deadline = Date.now() + 240_000;
  while (operation.status === "queued" || operation.status === "running") {
    if (Date.now() >= deadline)
      throw new Error("La solicitud aceptada no termino dentro del plazo E2E.");
    await page.waitForTimeout(Math.min(10000, Math.max(2000, operation.poll_after_ms || 2000)));
    const progress = await page.request.get(
      `/api/v1/chat/requests/${encodeURIComponent(sent.client_request_id)}`,
    );
    if (!progress.ok())
      throw new Error(`No fue posible consultar la solicitud: HTTP ${progress.status()}`);
    operation = await progress.json();
  }
  if (operation.status !== "completed")
    throw new Error(`La solicitud termino sin respuesta: ${operation.status}`);
  const reply = operation.response;
  if (
    !reply ||
    typeof reply.message_id !== "string" ||
    !reply.message_id ||
    typeof reply.conversation_id !== "string" ||
    !reply.conversation_id ||
    typeof reply.answer !== "string" ||
    !reply.answer.trim() ||
    typeof sent.client_request_id !== "string" ||
    !sent.client_request_id ||
    operation.conversation_id !== reply.conversation_id ||
    (sent.conversation_id !== null && sent.conversation_id !== reply.conversation_id)
  ) {
    throw new Error(
      "La respuesta no cumple el contrato del envio o pertenece a otra conversacion.",
    );
  }
  await expect(page.getByTestId("message-pending")).toBeHidden({ timeout: 240_000 });
  await expect(answers).toHaveCount(previousCount + 1);
  await expect(answers.last()).toHaveAttribute("data-message-id", reply.message_id);
  await expect(answers.last()).toBeVisible();
  return (await answers.last().innerText()).trim();
}

export async function logout(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Salir" }).click();
  await expect(page.getByRole("button", { name: "Entrar" })).toBeVisible();
}

/** Terminos que jamas deben aparecer en una respuesta a un usuario restringido. */
export const RESTRICTED_TERMS = [
  "politica-de-nomina",
  "proceso-de-reclutamiento",
  "reglamento-interior",
  "seguridad-higiene-y-ambiente",
];

export function assertNoRestrictedLeak(answer: string): void {
  const lowered = answer.toLowerCase();
  for (const term of RESTRICTED_TERMS) {
    expect(lowered, `la respuesta filtro la fuente restringida ${term}`).not.toContain(term);
  }
}
