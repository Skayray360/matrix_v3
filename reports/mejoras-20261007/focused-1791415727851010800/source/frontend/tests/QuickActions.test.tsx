/* Creado por Aldo Garcia. */
/** Pruebas de accesos rapidos: son prompts, nunca decisiones de autorizacion. */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { QuickActions } from "../src/components/QuickActions";

describe("QuickActions", () => {
  it("envia el prompt del tema elegido", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<QuickActions disabled={false} onSelect={onSelect} />);

    await user.click(screen.getByTestId("quick-action-prestaciones"));

    expect(onSelect).toHaveBeenCalledOnce();
    expect(onSelect.mock.calls[0][0]).toMatch(/prestaciones/i);
  });

  it("deshabilita todos los accesos durante una respuesta", () => {
    render(<QuickActions disabled onSelect={vi.fn()} />);
    expect(screen.getAllByRole("button").every((button) => button.hasAttribute("disabled"))).toBe(
      true,
    );
  });
});
