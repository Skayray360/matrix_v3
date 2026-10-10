/* Creado por Aldo Garcia. */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import { ChangePasswordDialog } from "../src/components/ChangePasswordDialog";
import { api, ApiError, getCsrfToken, setCsrfToken, type Me } from "../src/services/api";

const profile: Me = {
  user_id: "synthetic-local-user",
  username: "synthetic-local",
  display_name: "Cuenta local sintética",
  auth_source: "local",
  roles: [],
  permissions: [],
  allowed_categories: [],
  category_wildcard: false,
  csrf_token: "synthetic-new-csrf",
};

// jsdom no implementa la capa modal del navegador. La prueba visual usa el diálogo nativo.
beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    },
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.removeAttribute("open");
    },
  });
});

afterAll(() => {
  Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
});

beforeEach(() => {
  vi.spyOn(api, "me").mockResolvedValue(profile);
  vi.spyOn(api, "listConversations").mockResolvedValue([]);
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  setCsrfToken("");
});

async function fillPasswords(
  user: ReturnType<typeof userEvent.setup>,
  newPassword = "Nueva clave sintética 2026",
  confirmation = newPassword,
): Promise<void> {
  await user.type(screen.getByLabelText("Contraseña actual"), "Clave actual sintética");
  await user.type(screen.getByLabelText("Nueva contraseña"), newPassword);
  await user.type(screen.getByLabelText("Confirmar nueva contraseña"), confirmation);
}

describe("Cambio de contraseña local", () => {
  it.each(["local_test", "entra", "oidc"])(
    "conserva %s sin ofrecer cambio local",
    async (source) => {
      vi.mocked(api.me).mockResolvedValue({ ...profile, auth_source: source });
      render(<App />);
      await screen.findByTestId("identity-user");
      expect(screen.queryByRole("button", { name: "Cambiar contraseña" })).not.toBeInTheDocument();
    },
  );

  it.each([
    ["Corta", "Corta", "entre 16 y 128"],
    ["Clave actual sintética", "Clave actual sintética", "diferente de la actual"],
    [" Nueva clave sintética", " Nueva clave sintética", "espacios al inicio o al final"],
    ["Nueva clave sintética", "Confirmación diferente", "no coincide"],
  ])("valida antes de enviar una contraseña inválida: %s", async (next, confirmation, message) => {
    const change = vi.spyOn(api, "changePassword");
    const user = userEvent.setup();
    render(<ChangePasswordDialog onChanged={vi.fn()} onClose={vi.fn()} />);
    await fillPasswords(user, next, confirmation);
    await user.click(screen.getByRole("button", { name: "Guardar contraseña" }));
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(change).not.toHaveBeenCalled();
  });

  it("bloquea reenvíos hasta confirmar y elimina los campos tras el cambio", async () => {
    let accept!: (value: Me) => void;
    const change = vi.spyOn(api, "changePassword").mockReturnValue(
      new Promise<Me>((resolve) => {
        accept = resolve;
      }),
    );
    const changed = vi.fn();
    const user = userEvent.setup();
    render(<ChangePasswordDialog onChanged={changed} onClose={vi.fn()} />);
    expect(screen.getByLabelText("Contraseña actual")).toHaveFocus();
    await fillPasswords(user);
    await user.click(screen.getByRole("button", { name: "Guardar contraseña" }));
    expect(screen.getByRole("button", { name: "Guardando…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancelar" })).toBeDisabled();
    expect(screen.getByLabelText("Contraseña actual")).toBeDisabled();
    const form = screen.getByRole("button", { name: "Guardando…" }).closest("form");
    if (!form) throw new Error("Formulario ausente");
    fireEvent.submit(form);
    expect(change).toHaveBeenCalledExactlyOnceWith(
      "Clave actual sintética",
      "Nueva clave sintética 2026",
    );
    expect(changed).not.toHaveBeenCalled();
    await act(async () => accept(profile));
    expect(changed).toHaveBeenCalledWith(profile);
    expect(screen.getByRole("status")).toHaveTextContent("Las demás sesiones se cerraron");
    expect(screen.getByRole("button", { name: "Listo" })).toHaveFocus();
    expect(screen.queryByLabelText("Contraseña actual")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Nueva contraseña")).not.toBeInTheDocument();
  });

  it("permite corregir la contraseña actual sin cerrar la sesión ante 422", async () => {
    vi.spyOn(api, "changePassword").mockRejectedValue(
      new ApiError("validation_error", "La contrasena actual es incorrecta.", 422, "synthetic-ref"),
    );
    const expired = vi.fn();
    const changed = vi.fn();
    const user = userEvent.setup();
    render(
      <ChangePasswordDialog onChanged={changed} onClose={vi.fn()} onSessionExpired={expired} />,
    );
    await fillPasswords(user);
    await user.click(screen.getByRole("button", { name: "Guardar contraseña" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La contrasena actual es incorrecta.",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("synthetic-ref");
    expect(screen.getByRole("button", { name: "Guardar contraseña" })).toBeEnabled();
    expect(expired).not.toHaveBeenCalled();
    expect(changed).not.toHaveBeenCalled();
  });

  it("actualiza el perfil sin perder el borrador y limpia el formulario al cerrarlo", async () => {
    vi.spyOn(api, "changePassword").mockResolvedValue({
      ...profile,
      display_name: "Perfil renovado",
    });
    const user = userEvent.setup();
    render(<App />);
    const open = await screen.findByRole("button", { name: "Cambiar contraseña" });
    await user.type(screen.getByTestId("composer-input"), "Borrador que se conserva");
    await user.click(open);
    await fillPasswords(user);
    await user.click(screen.getByRole("button", { name: "Guardar contraseña" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Contraseña actualizada");
    expect(screen.getByTestId("identity-user")).toHaveTextContent("Perfil renovado");
    await user.click(screen.getByRole("button", { name: "Listo" }));
    expect(screen.getByTestId("composer-input")).toHaveValue("Borrador que se conserva");
    expect(open).toHaveFocus();
    await user.click(open);
    expect(screen.getByLabelText("Contraseña actual")).toHaveValue("");
    expect(screen.getByLabelText("Nueva contraseña")).toHaveValue("");
  });

  it("vuelve al acceso y elimina el CSRF si el servidor confirma sesión expirada", async () => {
    setCsrfToken("synthetic-old-csrf");
    vi.spyOn(api, "changePassword").mockRejectedValue(
      new ApiError("unauthenticated", "Sesión expirada", 401, null),
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Cambiar contraseña" }));
    await fillPasswords(user);
    await user.click(screen.getByRole("button", { name: "Guardar contraseña" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Entrar" })).toBeInTheDocument());
    expect(screen.getByRole("status")).toHaveTextContent("Su sesión expiró");
    expect(screen.queryByTestId("identity-user")).not.toBeInTheDocument();
    expect(getCsrfToken()).toBe("");
  });
});
