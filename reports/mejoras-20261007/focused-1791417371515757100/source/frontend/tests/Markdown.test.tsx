/* Creado por Aldo Garcia. */
/**
 * Pruebas del renderizador Markdown.
 *
 * El caso critico es el de XSS: el renderizador construye elementos de React, de
 * modo que cualquier HTML incrustado en una respuesta del modelo o en un
 * documento corporativo debe aparecer como TEXTO, nunca como marcado activo.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "../src/security/Markdown";

describe("Markdown", () => {
  it("conserva el final de respuestas extensas y palabras sin espacios", () => {
    const paragraphs = Array.from(
      { length: 120 },
      (_, n) => `Párrafo ${n}: ${"contenido ".repeat(50)}`,
    );
    const word = "x".repeat(2000);
    const { container } = render(
      <Markdown text={`${paragraphs.join("\n\n")}\n\n${word}\n\nFIN_COMPLETO`} />,
    );
    for (const paragraph of paragraphs) expect(container.textContent).toContain(paragraph.trim());
    expect(container.textContent).toContain(word);
    expect(screen.getByText("FIN_COMPLETO")).toBeInTheDocument();
  });

  it("preserva la numeración explícita y separa tipos de lista", () => {
    render(<Markdown text={"5. Quinta condición\n9. Novena condición\n- Excepción final"} />);
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveAttribute("value", "5");
    expect(items[1]).toHaveAttribute("value", "9");
    expect(screen.getAllByRole("list")).toHaveLength(2);
    expect(items[2]).toHaveTextContent("Excepción final");
  });

  it("conserva citas verificadas dentro de negrita y permite recorrer tablas", () => {
    render(
      <Markdown
        text={"**Regla [[doc#0]]**\n\n| Campo | Valor |\n| --- | --- |\n| Fin | Completo |"}
        knownSources={new Set(["doc#0"])}
        sourceNumbers={new Map([["doc#0", 1]])}
      />,
    );
    expect(screen.getByTestId("citation")).toHaveTextContent("1");
    expect(screen.getByRole("region", { name: "Tabla de la respuesta" })).toHaveAttribute(
      "tabindex",
      "0",
    );
    expect(screen.getByText("Completo")).toBeInTheDocument();
  });

  it("renderiza parrafos, negrita y listas", () => {
    render(<Markdown text={"Politica **vigente**\n\n- uno\n- dos"} />);
    expect(screen.getByText("vigente").tagName).toBe("STRONG");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("no ejecuta ni inyecta HTML incrustado", () => {
    const malicious =
      '<img src=x onerror="window.__pwned=true"> <script>window.__pwned=true</script>';
    const { container } = render(<Markdown text={malicious} />);

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
    // El marcado se muestra como texto literal.
    expect(container.textContent).toContain("<img src=x");
  });

  it("marca como cita solo los source_id conocidos", () => {
    render(
      <Markdown
        text="Son 20 dias [[prestaciones/politica-vacaciones.md#2]] y no [[nomina/secreto.md#9]]."
        knownSources={new Set(["prestaciones/politica-vacaciones.md#2"])}
      />,
    );
    const citations = screen.getAllByTestId("citation");
    expect(citations).toHaveLength(1);
    expect(citations[0]).toHaveAttribute("title", "prestaciones/politica-vacaciones.md#2");
    // La cita desconocida queda como texto plano, sin apariencia de fuente.
    expect(screen.getByText(/nomina\/secreto\.md#9/)).toBeInTheDocument();
  });

  it("renderiza tablas dentro de un contenedor con scroll propio", () => {
    const { container } = render(
      <Markdown text={"| Antiguedad | Dias |\n| --- | --- |\n| 1 anio | 12 |"} />,
    );
    expect(container.querySelector(".table-scroll")).not.toBeNull();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader")).toHaveLength(2);
  });

  it("muestra una tabla incompleta como texto sin bloquear la interfaz", () => {
    const { container } = render(
      <Markdown text={"| Columna sin cerrar\nTexto posterior\n\nRespuesta disponible"} />,
    );
    expect(container.textContent).toContain("| Columna sin cerrar");
    expect(screen.getByText("Respuesta disponible")).toBeInTheDocument();
    expect(container.querySelector("table")).toBeNull();
  });

  it("conserva citas privadas y rutas PDF largas solo si estan autorizadas", () => {
    const privateId = "__private__/documento-123/curriculum.pdf#2";
    const pdfId = `prestaciones/${"carpeta/".repeat(40)}politica.pdf#0`;
    render(
      <Markdown
        text={`Evidencia [[${privateId}]] y [[${pdfId}]]; desconocida [[__private__/otro/cv.pdf#0]].`}
        knownSources={new Set([privateId, pdfId])}
        sourceNumbers={
          new Map([
            [privateId, 1],
            [pdfId, 2],
          ])
        }
      />,
    );
    const citations = screen.getAllByTestId("citation");
    expect(citations).toHaveLength(2);
    expect(citations[0]).toHaveAttribute("title", privateId);
    expect(citations[1]).toHaveAttribute("title", pdfId);
    expect(citations[1]).toHaveTextContent("2");
    expect(screen.getByText(/\[\[__private__\/otro\/cv.pdf#0\]\]/)).toBeInTheDocument();
  });
});
