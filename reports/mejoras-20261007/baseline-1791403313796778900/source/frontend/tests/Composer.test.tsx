/* Creado por Aldo Garcia. */
/** Pruebas del composer: envio, teclado y visibilidad del estado de adjuntos. */

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Composer } from "../src/components/Composer";

describe("Composer", () => {
  it("envia el mensaje con Enter y limpia el campo", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer disabled={false} attachments={[]} onSend={onSend} onUpload={vi.fn()} />);

    const input = screen.getByTestId("composer-input");
    await user.type(input, "Cuantos dias de vacaciones tengo?");
    await user.keyboard("{Enter}");

    expect(onSend).toHaveBeenCalledWith("Cuantos dias de vacaciones tengo?");
    expect(input).toHaveValue("");
  });

  it("Shift+Enter no envia", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer disabled={false} attachments={[]} onSend={onSend} onUpload={vi.fn()} />);

    await user.type(screen.getByTestId("composer-input"), "linea uno");
    await user.keyboard("{Shift>}{Enter}{/Shift}");

    expect(onSend).not.toHaveBeenCalled();
  });

  it("deshabilita el envio mientras hay una respuesta en curso", () => {
    render(<Composer disabled attachments={[]} onSend={vi.fn()} onUpload={vi.fn()} />);
    expect(screen.getByTestId("send-button")).toBeDisabled();
  });

  it("no acepta archivos arrastrados cuando esta deshabilitado", () => {
    const onUpload = vi.fn();
    render(<Composer disabled attachments={[]} onSend={vi.fn()} onUpload={onUpload} />);
    fireEvent.drop(screen.getByTestId("composer-input"), {
      dataTransfer: { files: [new File(["contenido"], "manual.txt")] },
    });
    expect(onUpload).not.toHaveBeenCalled();
  });

  it("muestra el estado de cada adjunto", () => {
    render(
      <Composer
        disabled={false}
        attachments={[
          {
            id: "d1",
            filename: "manual.pdf",
            status: "indexed",
            chunk_count: 4,
            scope: "conversation",
            category: null,
            error_message: null,
          },
        ]}
        onSend={vi.fn()}
        onUpload={vi.fn()}
      />,
    );
    expect(screen.getByTestId("attachments")).toHaveTextContent("manual.pdf");
    expect(screen.getByTestId("attachments")).toHaveTextContent("indexed");
  });

  it("advierte que el adjunto es privado de la conversacion", () => {
    render(<Composer disabled={false} attachments={[]} onSend={vi.fn()} onUpload={vi.fn()} />);
    expect(screen.getByText(/solo en esta conversacion/i)).toBeInTheDocument();
  });
});
