/* Creado por Aldo Garcia. */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LoginPage } from "../src/pages/LoginPage";
import { api, ApiError, type Me } from "../src/services/api";

const profile: Me = {
  user_id: "ui",
  username: "usuario-ui",
  display_name: "MatrixR1",
  auth_source: "local_test",
  roles: [],
  permissions: [],
  allowed_categories: ["prestaciones"],
  category_wildcard: false,
  csrf_token: "csrf-sintetico",
};
afterEach(() => vi.restoreAllMocks());

describe("Login visual con autenticacion Matrix", () => {
  it("conserva el acceso corporativo del backend", () => {
    render(<LoginPage onAuthenticated={vi.fn()} />);
    expect(screen.getByRole("link", { name: "Continuar con acceso corporativo" })).toHaveAttribute(
      "href",
      "/api/v1/auth/login",
    );
    expect(screen.queryByRole("link", { name: /vertex/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Entrar" })).toBeEnabled();
  });
  it("espera la autenticacion antes de abrir el chat y bloquea reenvios", async () => {
    let accept!: (value: Me) => void;
    vi.spyOn(api, "localLogin").mockReturnValue(
      new Promise<Me>((resolve) => {
        accept = resolve;
      }),
    );
    const authenticated = vi.fn();
    const user = userEvent.setup();
    render(<LoginPage onAuthenticated={authenticated} />);
    await user.type(screen.getByLabelText("Usuario"), "usuario-ui");
    await user.type(screen.getByLabelText("Contrasena"), "dato-sintetico");
    await user.click(screen.getByRole("button", { name: "Entrar" }));
    expect(api.localLogin).toHaveBeenCalledWith("usuario-ui", "dato-sintetico");
    expect(screen.getByRole("button", { name: "Verificando..." })).toBeDisabled();
    expect(authenticated).not.toHaveBeenCalled();
    accept(profile);
    await waitFor(() => expect(authenticated).toHaveBeenCalledTimes(1));
  });
  it("muestra el rechazo del servidor y permite corregir el acceso", async () => {
    vi.spyOn(api, "localLogin").mockRejectedValue(
      new ApiError("invalid_credentials", "Usuario o contraseña incorrectos.", 401, null),
    );
    const authenticated = vi.fn();
    render(<LoginPage onAuthenticated={authenticated} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Entrar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Usuario o contraseña incorrectos.");
    expect(screen.getByRole("button", { name: "Entrar" })).toBeEnabled();
    expect(authenticated).not.toHaveBeenCalled();
  });
  it("mostrar y ocultar no modifica la contraseña", async () => {
    const user = userEvent.setup();
    render(<LoginPage onAuthenticated={vi.fn()} />);
    const field = screen.getByLabelText("Contrasena");
    await user.type(field, "dato-sintetico");
    await user.click(screen.getByRole("button", { name: "Mostrar" }));
    expect(field).toHaveAttribute("type", "text");
    expect(field).toHaveValue("dato-sintetico");
    expect(screen.getByRole("button", { name: "Ocultar" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Ocultar" }));
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveValue("dato-sintetico");
  });
});
