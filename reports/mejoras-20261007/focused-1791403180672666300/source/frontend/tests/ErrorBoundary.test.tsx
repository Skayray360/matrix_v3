/* Creado por Aldo Garcia. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "../src/components/ErrorBoundary";

function BrokenComponent(): JSX.Element {
  throw new Error("Detalle privado: expediente de RH y traza interna");
}

function preventExpectedError(event: ErrorEvent): void {
  if (
    event.error instanceof Error &&
    event.error.message === "Detalle privado: expediente de RH y traza interna"
  ) {
    event.preventDefault();
  }
}

beforeEach(() => {
  // React informa del fallo simulado en desarrollo; no es parte de la interfaz.
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  window.addEventListener("error", preventExpectedError);
});
afterEach(() => {
  window.removeEventListener("error", preventExpectedError);
  vi.restoreAllMocks();
});

describe("ErrorBoundary", () => {
  it("conserva la interfaz cuando el render es correcto", () => {
    render(
      <ErrorBoundary>
        <p>Conversación disponible</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("Conversación disponible")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sustituye la pantalla en blanco por recuperación sin exponer el error", () => {
    render(
      <ErrorBoundary>
        <BrokenComponent />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("No se pudo mostrar Matrix RH");
    expect(screen.getByRole("button", { name: "Recargar Matrix RH" })).toBeEnabled();
    expect(screen.queryByText(/Detalle privado/)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("expediente de RH");
  });

  it("permite recuperar la aplicación mediante recarga", async () => {
    const reload = vi.fn();
    render(
      <ErrorBoundary onReload={reload}>
        <BrokenComponent />
      </ErrorBoundary>,
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "Recargar Matrix RH" }));
    expect(reload).toHaveBeenCalledOnce();
  });
});
