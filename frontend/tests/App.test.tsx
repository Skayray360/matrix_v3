/* Creado por Aldo Garcia. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { api, ApiError } from "../src/services/api";

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue({
    user_id: "synthetic-session",
    username: "synthetic",
    display_name: "Usuario sintético",
    auth_source: "local_test",
    roles: [],
    permissions: [],
    allowed_categories: [],
    category_wildcard: false,
    csrf_token: "synthetic-csrf",
  });
  vi.spyOn(api, "listConversations").mockResolvedValue([]);
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Confirmación del cierre de sesión", () => {
  it("distingue un servidor no disponible de una sesión vencida y permite reintentar", async () => {
    const user = userEvent.setup();
    vi.mocked(api.me).mockRejectedValueOnce(
      new ApiError("request_timeout", "El servidor tardó demasiado", 408, "synthetic-timeout"),
    );
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo verificar su sesión");
    expect(screen.getByRole("alert")).toHaveTextContent("synthetic-timeout");
    expect(screen.queryByRole("button", { name: "Entrar" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reintentar conexión" }));
    expect(await screen.findByTestId("identity-user")).toHaveTextContent("Usuario sintético");
    expect(api.me).toHaveBeenCalledTimes(2);
  });

  it("presenta acceso sin un error de transporte cuando /me confirma 401", async () => {
    vi.mocked(api.me).mockRejectedValueOnce(
      new ApiError("unauthenticated", "Inicie sesión", 401, null),
    );
    render(<App />);
    expect(await screen.findByRole("button", { name: "Entrar" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("no simula logout si falla revocación y permite repetirlo", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "logout")
      .mockRejectedValueOnce(new ApiError("request_timeout", "Conexión interrumpida", 408, null))
      .mockResolvedValueOnce(undefined);
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Salir" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No se pudo confirmar el cierre de sesión",
    );
    expect(screen.getByTestId("identity-user")).toHaveTextContent("Usuario sintético");
    expect(screen.queryByRole("button", { name: "Entrar" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Salir" }));
    expect(await screen.findByRole("button", { name: "Entrar" })).toBeInTheDocument();
    expect(api.logout).toHaveBeenCalledTimes(2);
  });

  it("vuelve a login cuando el backend confirma que la sesión ya expiró", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "logout").mockRejectedValue(
      new ApiError("unauthenticated", "Sesión vencida", 401, null),
    );
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Salir" }));
    expect(await screen.findByRole("button", { name: "Entrar" })).toBeInTheDocument();
  });
});
