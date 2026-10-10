/* Creado por Aldo Garcia. */
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, getCsrfToken, setCsrfToken, type Me } from "../src/services/api";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken("");
});

describe("Transporte del cambio de contraseña", () => {
  it("envía solo contraseña actual/nueva con cookie y CSRF y conserva la rotación devuelta", async () => {
    const renewed: Me = {
      user_id: "synthetic-local",
      username: "synthetic",
      display_name: "Cuenta sintética",
      auth_source: "local",
      roles: [],
      permissions: [],
      allowed_categories: [],
      category_wildcard: false,
      csrf_token: "synthetic-renewed-csrf",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(renewed), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("synthetic-original-csrf");
    await expect(
      api.changePassword("Anterior sintética", "Nueva clave sintética"),
    ).resolves.toEqual(renewed);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/local/change-password",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": "synthetic-original-csrf" },
        body: JSON.stringify({
          current_password: "Anterior sintética",
          new_password: "Nueva clave sintética",
        }),
      }),
    );
    expect(getCsrfToken()).toBe("synthetic-renewed-csrf");
  });

  it("conserva el CSRF actual cuando el servidor rechaza la contraseña", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ code: "validation_error", message: "Contraseña actual incorrecta" }),
            { status: 422 },
          ),
        ),
    );
    setCsrfToken("synthetic-original-csrf");
    await expect(
      api.changePassword("Anterior errónea", "Nueva clave sintética"),
    ).rejects.toMatchObject({ status: 422 });
    expect(getCsrfToken()).toBe("synthetic-original-csrf");
  });
});
