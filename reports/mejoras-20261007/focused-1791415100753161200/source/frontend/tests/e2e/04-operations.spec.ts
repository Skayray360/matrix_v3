/* Creado por Aldo Garcia. */
/**
 * E2E 11, 12, 15, 16, 19, 31: routing de modelos, resiliencia, categorias nuevas
 * y bloqueo del modo local en produccion.
 *
 * El routing se observa por la vista administrativa de diagnostico y por la
 * auditoria, no exponiendo el modelo al usuario final (seccion 14).
 */

import { expect, test } from "@playwright/test";

import { ADMIN_USER, ask, login } from "./helpers";

test.describe("Operacion y resiliencia", () => {
  test("E2E-11/12 las rutas usan los modelos configurados para cada solicitud", async ({
    page,
  }) => {
    test.setTimeout(540_000); // Dos generaciones reales; no es un SLO de rendimiento.
    await login(page, ADMIN_USER);
    const diagnostics = await (await page.request.get("/api/v1/admin/diagnostics")).json();

    for (const role of ["fast", "deep", "embedding"]) {
      expect(typeof diagnostics.models[role]).toBe("string");
      expect(diagnostics.models[role].trim()).not.toBe("");
    }
    expect(diagnostics.models.embedding_dimension).toBe(diagnostics.rag.RAG_EMBEDDING_DIMENSION);
    expect(diagnostics.models.embedding_dimension).toBeGreaterThan(0);

    async function verifyRoute(question: string, expectedModel: string) {
      await page.getByTestId("new-conversation").click();
      const [response] = await Promise.all([
        page.waitForResponse(
          (candidate) => {
            const request = candidate.request();
            return (
              request.method() === "POST" &&
              new URL(candidate.url()).pathname === "/api/v1/chat/submit" &&
              request.postDataJSON()?.message === question
            );
          },
          { timeout: 240_000 },
        ),
        ask(page, question),
      ]);
      const requestId = response.headers()["x-request-id"];
      expect(requestId).toBeTruthy();
      const audit = await (await page.request.get("/api/v1/admin/audit/recent?limit=25")).json();
      const event = (audit.events as { request_id: string; selected_model: string | null }[]).find(
        (entry) => entry.request_id === requestId && entry.selected_model,
      );
      expect(event, "no existe auditoria de la solicitud actual").toBeDefined();
      expect(event?.selected_model).toBe(expectedModel);
    }

    // Consulta simple: debe resolverse por la ruta rapida.
    await verifyRoute(
      "Cuantos dias de vacaciones corresponden a 5 anios de antiguedad?",
      diagnostics.models.fast,
    );

    // Consulta comparativa multi-categoria: debe escalar a la ruta profunda.
    await verifyRoute(
      "Compara en detalle las reglas de prestaciones frente a las de nomina y explica sus implicaciones para un empleado de confianza.",
      diagnostics.models.deep,
    );
  });

  test("E2E-13 el diagnostico expone parametros del RAG y ningun secreto", async ({ page }) => {
    await login(page, ADMIN_USER);
    const response = await page.request.get("/api/v1/admin/diagnostics");
    expect(response.status()).toBe(200);
    const body = await response.json();

    expect(body.rag.RAG_TOP_K).toBeGreaterThan(0);
    expect(body.rag.RAG_FETCH_K).toBeGreaterThanOrEqual(body.rag.RAG_TOP_K);
    expect(body.rag.RAG_CHUNK_OVERLAP_TOKENS).toBeGreaterThanOrEqual(0);
    expect(body.rag.RAG_CHUNK_SIZE_TOKENS).toBeGreaterThan(body.rag.RAG_CHUNK_OVERLAP_TOKENS);
    expect(body.rag.RAG_MIN_SIMILARITY).toBeGreaterThanOrEqual(0);
    expect(body.rag.RAG_MIN_SIMILARITY).toBeLessThanOrEqual(1);
    expect(body.rag.RAG_MMR_LAMBDA).toBeGreaterThanOrEqual(0);
    expect(body.rag.RAG_MMR_LAMBDA).toBeLessThanOrEqual(1);
    expect(body.rag.OLLAMA_FAST_MODEL).toBe(body.models.fast);
    expect(body.rag.OLLAMA_DEEP_MODEL).toBe(body.models.deep);

    const serialized = JSON.stringify(body).toLowerCase();
    for (const forbidden of ["password", "client_secret", "mysql+pymysql", "argon2", "api_key"]) {
      expect(serialized, `el diagnostico expuso ${forbidden}`).not.toContain(forbidden);
    }
  });

  test("E2E-19 una categoria nueva queda deny-by-default hasta tener politica", async ({
    page,
  }) => {
    await login(page, ADMIN_USER);
    const summary = await (await page.request.get("/api/v1/admin/knowledge/summary")).json();
    const known = summary.known_categories as string[];

    // Las cinco semillas son conocidas...
    for (const seed of [
      "prestaciones",
      "nomina",
      "reclutamiento",
      "relaciones_laborales",
      "salud_ambiental",
    ]) {
      expect(known).toContain(seed);
    }
    // ...y una categoria marcada como no elegible por wildcard sigue fuera del
    // alcance efectivo incluso del administrador de negocio.
    const me = await (await page.request.get("/api/v1/me")).json();
    expect(me.allowed_categories).not.toContain("investigaciones_internas");
  });

  test("E2E-15/16 los errores no filtran stack traces ni secretos", async ({ page }) => {
    await login(page, ADMIN_USER);

    // Recurso inexistente: respuesta tipada y sin detalles internos.
    const missing = await page.request.get("/api/v1/conversations/no-existe-este-id");
    expect(missing.status()).toBe(404);
    const body = await missing.text();
    expect(body).not.toContain("Traceback");
    expect(body).not.toContain("sqlalchemy");
    expect(body).not.toContain("mysql");
    expect(JSON.parse(body).code).toBe("not_found");
  });

  test("las cabeceras de seguridad estan presentes", async ({ page }) => {
    const response = await page.request.get("/api/v1/health");
    const headers = response.headers();
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["x-frame-options"]).toBe("DENY");
    expect(headers["content-security-policy"]).toContain("frame-ancestors 'none'");
    expect(headers["content-security-policy"]).toContain("object-src 'none'");
    expect(headers["referrer-policy"]).toBe("no-referrer");
  });

  test("la interfaz es usable en viewport movil sin scroll horizontal", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 780 });
    await login(page, ADMIN_USER);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    );
    expect(overflow).toBe(false);
    await expect(page.getByRole("button", { name: "Conversaciones", exact: true })).toBeVisible();
  });
});
