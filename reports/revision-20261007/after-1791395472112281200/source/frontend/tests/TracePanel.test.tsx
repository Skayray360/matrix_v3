/* Creado por Aldo Garcia. */
/** La trazabilidad solo presenta metadatos autorizados recibidos del backend. */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TracePanel } from "../src/components/TracePanel";

describe("TracePanel", () => {
  it("muestra estado inicial sin inventar evidencia", () => {
    render(<TracePanel trace={null} />);
    expect(screen.getByText("Sin ejecutar")).toBeInTheDocument();
    expect(screen.getByText(/fuentes autorizadas/i)).toBeInTheDocument();
  });

  it("no etiqueta como documentada una respuesta sin fuentes", () => {
    render(
      <TracePanel
        trace={{
          conversationId: "conv-general",
          intent: "identity",
          grounded: true,
          latencyMs: 12,
          sources: [],
        }}
      />,
    );

    expect(screen.getByText("Sin fuentes documentales")).toBeInTheDocument();
    expect(screen.queryByText("Con fuentes")).not.toBeInTheDocument();
  });

  it("muestra la fuente y latencia de la ultima respuesta", () => {
    render(
      <TracePanel
        trace={{
          conversationId: "12345678-abcd-efgh-ijkl-123456789012",
          intent: "document_question",
          grounded: true,
          latencyMs: 640,
          sources: [
            {
              source_id: "general/manual.md#0",
              category: "general",
              filename: "manual.md",
              section: "Objetivo",
              page_or_sheet: "",
              score: 0.8,
              label: "Manual general",
              scope: "corporate",
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("Con fuentes")).toBeInTheDocument();
    expect(screen.getByText("640 ms")).toBeInTheDocument();
    expect(screen.getByText("Manual general")).toBeInTheDocument();
    expect(screen.getByText("general")).toBeInTheDocument();
    expect(screen.getByText("Fuentes citadas")).toBeInTheDocument();
    expect(screen.queryByText(/Cada afirmacion/)).not.toBeInTheDocument();
  });

  it("distingue procedencia general sin interpretar grounded como veracidad", () => {
    render(
      <TracePanel
        trace={{
          conversationId: "conv-general",
          intent: "documental",
          answerBasis: "general",
          grounded: true,
          latencyMs: 12,
          sources: [],
        }}
      />,
    );
    expect(screen.getByText("Conocimiento general")).toBeInTheDocument();
    expect(screen.getByText(/sin documentación de la empresa/)).toBeInTheDocument();
    expect(screen.queryByText(/validada|verificada/i)).not.toBeInTheDocument();
  });

  it("explica cuándo las fuentes se complementan con conocimiento general", () => {
    render(
      <TracePanel
        trace={{
          conversationId: "conv-mixed",
          intent: "documental",
          answerBasis: "mixed",
          grounded: true,
          latencyMs: 15,
          sources: [
            {
              source_id: "prestaciones/manual.pdf#0",
              category: "prestaciones",
              filename: "manual.pdf",
              section: "",
              page_or_sheet: "1",
              score: 0.5,
              label: "Manual",
              scope: "corporate",
            },
          ],
        }}
      />,
    );
    expect(screen.getByText("Respuesta mixta")).toBeInTheDocument();
    expect(screen.getByText(/combina fuentes citadas y conocimiento general/)).toBeInTheDocument();
    expect(screen.getByText("Manual")).toBeInTheDocument();
  });

  it("una procedencia mixta no inventa fuentes que no llegaron en la respuesta", () => {
    render(
      <TracePanel
        trace={{
          conversationId: "conv-mixed",
          intent: "documental",
          answerBasis: "mixed",
          grounded: true,
          latencyMs: 15,
          sources: [],
        }}
      />,
    );
    expect(screen.getByText("Respuesta mixta")).toBeInTheDocument();
    expect(screen.getByText(/No hay fuentes documentales visibles/)).toBeInTheDocument();
    expect(screen.queryByText(/combina fuentes citadas/)).not.toBeInTheDocument();
  });
});
