/* Creado por Aldo Garcia. */
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MessageList, type DisplayMessage } from "../src/components/MessageList";

const notice = "Respuesta general: no basada en documentación de la empresa";
const message: DisplayMessage = {
  id: "reply",
  role: "assistant",
  content: "Respuesta de prueba",
  sources: [],
};
const source = {
  source_id: "prestaciones/vacaciones.pdf#2",
  category: "prestaciones",
  filename: "vacaciones.pdf",
  section: "Vacaciones",
  page_or_sheet: "2",
  score: 0.8,
  label: "Política de vacaciones",
  scope: "corporate",
};

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
});

describe("Origen de las respuestas", () => {
  it("identifica claramente una respuesta general", () => {
    render(
      <MessageList
        messages={[{ ...message, intent: "general" }]}
        pending={false}
        displayName="Prueba"
      />,
    );
    expect(screen.getByTestId("general-notice")).toHaveTextContent(notice);
    expect(screen.getByText(message.content)).toBeInTheDocument();
  });

  it.each(["documental", "conversational", null, undefined])(
    "no inventa origen general para intent %s",
    (intent) => {
      render(
        <MessageList messages={[{ ...message, intent }]} pending={false} displayName="Prueba" />,
      );
      expect(screen.queryByText(notice)).not.toBeInTheDocument();
    },
  );

  it("no atribuye una pregunta del usuario al modelo", () => {
    render(
      <MessageList
        messages={[{ ...message, role: "user", intent: "general" }]}
        pending={false}
        displayName="Prueba"
      />,
    );
    expect(screen.queryByText(notice)).not.toBeInTheDocument();
  });

  it("usa la procedencia general explícita aunque la intención sea documental", () => {
    render(
      <MessageList
        messages={[{ ...message, intent: "documental", answer_basis: "general" }]}
        pending={false}
        displayName="Prueba"
      />,
    );
    expect(screen.getByTestId("general-notice")).toHaveTextContent(notice);
    expect(screen.queryByTestId("sources")).not.toBeInTheDocument();
  });

  it("distingue conocimiento general complementario de las fuentes realmente citadas", () => {
    render(
      <MessageList
        messages={[
          {
            ...message,
            content: `La política dice esto [[${source.source_id}]]. Como orientación general, considere lo siguiente.`,
            intent: "documental",
            answer_basis: "mixed",
            sources: [source],
          },
        ]}
        pending={false}
        displayName="Prueba"
      />,
    );
    expect(screen.getByTestId("answer-basis-notice")).toHaveTextContent(
      "combina las fuentes citadas con conocimiento general",
    );
    expect(screen.getByText("1 fuente citada")).toBeInTheDocument();
    expect(screen.getByTestId("citation")).toHaveAttribute("title", source.source_id);
    expect(screen.getByTestId("sources")).toHaveTextContent(source.label);
    expect(screen.queryByText(/validada|verificada/i)).not.toBeInTheDocument();
  });

  it("una respuesta insuficiente no se convierte en general por la intención heredada", () => {
    render(
      <MessageList
        messages={[{ ...message, intent: "general", answer_basis: "insufficient" }]}
        pending={false}
        displayName="Prueba"
      />,
    );
    expect(screen.getByTestId("answer-basis-notice")).toHaveTextContent(
      "Documentación insuficiente",
    );
    expect(screen.queryByTestId("general-notice")).not.toBeInTheDocument();
  });

  it("no afirma que una respuesta mixta cita fuentes cuando no recibe referencias visibles", () => {
    render(
      <MessageList
        messages={[{ ...message, answer_basis: "mixed" }]}
        pending={false}
        displayName="Prueba"
      />,
    );
    expect(screen.getByTestId("answer-basis-notice")).toHaveTextContent(
      "las fuentes documentales no están disponibles",
    );
    expect(screen.queryByTestId("sources")).not.toBeInTheDocument();
    expect(screen.queryByText(/fuentes citadas/)).not.toBeInTheDocument();
  });
});
