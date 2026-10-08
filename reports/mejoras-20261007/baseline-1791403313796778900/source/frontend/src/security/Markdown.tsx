/* Creado por Aldo Garcia. */
/**
 * Renderizador de Markdown seguro por construccion.
 *
 * En lugar de convertir Markdown a HTML y confiar en un sanitizador, este
 * componente construye **elementos de React** directamente. Nunca se usa
 * `dangerouslySetInnerHTML`, por lo que no existe ninguna via por la que un
 * documento corporativo o una respuesta del modelo puedan inyectar HTML o
 * script: cualquier `<img onerror=...>` que venga en el texto se muestra como
 * texto literal, que es exactamente lo que debe pasar.
 *
 * Subconjunto soportado: encabezados, parrafos, listas, tablas, codigo en linea
 * y bloques de codigo, negrita, cursiva y las citas propias de Matrix RH
 * `[[source_id]]`.
 */

import { Fragment, type ReactNode } from "react";

type Props = {
  text: string;
  /** Citas validas. Una cita fuera de esta lista se muestra como texto plano. */
  knownSources?: Set<string>;
  /**
   * Numero que le corresponde a cada `source_id` en la lista de fuentes del
   * mensaje. Sin este mapa la cita muestra el `source_id` completo, que es el
   * comportamiento original: util para pruebas y para cualquier uso suelto del
   * renderizador, ilegible en mitad de un parrafo.
   */
  sourceNumbers?: Map<string, number>;
};

const CITATION = /\[\[([^\]]{1,1024})\]\]/g;
const INLINE = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;

/** Aplica formato en linea (negrita, cursiva, codigo) y marca las citas. */
function renderInline(
  text: string,
  knownSources?: Set<string>,
  sourceNumbers?: Map<string, number>,
): ReactNode[] {
  const nodes: ReactNode[] = [];
  let key = 0;

  const withCitations = (chunk: string): void => {
    let lastIndex = 0;
    CITATION.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = CITATION.exec(chunk)) !== null) {
      if (match.index > lastIndex) {
        nodes.push(<Fragment key={key++}>{chunk.slice(lastIndex, match.index)}</Fragment>);
      }
      const sourceId = match[1].trim();
      const known = !knownSources || knownSources.has(sourceId);
      const number = sourceNumbers?.get(sourceId);
      nodes.push(
        known ? (
          <sup key={key++} className="citation" title={sourceId} data-testid="citation">
            {number === undefined ? `[${sourceId}]` : number}
          </sup>
        ) : (
          // Cita desconocida: se muestra como texto plano, sin apariencia de
          // fuente verificada. El backend ya deberia haberla rechazado; esto es
          // la ultima red visual.
          <Fragment key={key++}>{match[0]}</Fragment>
        ),
      );
      lastIndex = match.index + match[0].length;
    }
    if (lastIndex < chunk.length) {
      nodes.push(<Fragment key={key++}>{chunk.slice(lastIndex)}</Fragment>);
    }
  };

  for (const part of text.split(INLINE)) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      nodes.push(
        <strong key={key++}>{renderInline(part.slice(2, -2), knownSources, sourceNumbers)}</strong>,
      );
    } else if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      nodes.push(
        <em key={key++}>{renderInline(part.slice(1, -1), knownSources, sourceNumbers)}</em>,
      );
    } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      nodes.push(<code key={key++}>{part.slice(1, -1)}</code>);
    } else {
      withCitations(part);
    }
  }
  return nodes;
}

function parseTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

export function Markdown({ text, knownSources, sourceNumbers }: Props): JSX.Element {
  const lines = (text ?? "").split("\n");
  const blocks: ReactNode[] = [];
  let key = 0;
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    // Bloque de codigo cercado
    if (line.trimStart().startsWith("```")) {
      const buffer: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trimStart().startsWith("```")) {
        buffer.push(lines[index]);
        index += 1;
      }
      index += 1;
      blocks.push(
        <pre key={key++} className="code-block">
          <code>{buffer.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    // Encabezado
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = Math.min(6, heading[1].length);
      const Tag = `h${level + 2 > 6 ? 6 : level + 2}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={key++}>{renderInline(heading[2], knownSources, sourceNumbers)}</Tag>);
      index += 1;
      continue;
    }

    // Tabla
    if (line.trim().startsWith("|") && line.trim().endsWith("|")) {
      const rows: string[][] = [];
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        const current = lines[index].trim();
        if (!/^\|[\s:\-|]+\|$/.test(current)) {
          rows.push(parseTableRow(current));
        }
        index += 1;
      }
      if (rows.length > 0) {
        const [header, ...body] = rows;
        blocks.push(
          <div
            key={key++}
            className="table-scroll"
            role="region"
            aria-label="Tabla de la respuesta"
            tabIndex={0}
          >
            <table>
              <thead>
                <tr>
                  {header.map((cell, i) => (
                    <th key={i}>{renderInline(cell, knownSources, sourceNumbers)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {body.map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, c) => (
                      <td key={c}>{renderInline(cell, knownSources, sourceNumbers)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>,
        );
      }
      continue;
    }

    // Lista
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: { text: string; number?: number }[] = [];
      while (
        index < lines.length &&
        /^\s*([-*+]|\d+[.)])\s+/.test(lines[index]) &&
        /^\s*\d+[.)]\s+/.test(lines[index]) === ordered
      ) {
        const number = /^\s*(\d+)[.)]\s+/.exec(lines[index]);
        items.push({
          text: lines[index].replace(/^\s*([-*+]|\d+[.)])\s+/, ""),
          number: number ? Number(number[1]) : undefined,
        });
        index += 1;
      }
      const ListTag = ordered ? "ol" : "ul";
      blocks.push(
        <ListTag key={key++}>
          {items.map((item, i) => (
            <li key={i} value={item.number}>
              {renderInline(item.text, knownSources, sourceNumbers)}
            </li>
          ))}
        </ListTag>,
      );
      continue;
    }

    // Linea en blanco
    if (!line.trim()) {
      index += 1;
      continue;
    }

    // Parrafo: se agrupan lineas consecutivas
    const paragraph: string[] = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^(#{1,6})\s+/.test(lines[index]) &&
      // Un fragmento incompleto de tabla sigue siendo texto. Rechazar cualquier
      // linea que empiece por | sin consumirla dejaba este parser en un bucle.
      !(lines[index].trim().startsWith("|") && lines[index].trim().endsWith("|")) &&
      !lines[index].trimStart().startsWith("```") &&
      !/^\s*([-*+]|\d+[.)])\s+/.test(lines[index])
    ) {
      paragraph.push(lines[index]);
      index += 1;
    }
    blocks.push(
      <p key={key++}>{renderInline(paragraph.join(" "), knownSources, sourceNumbers)}</p>,
    );
  }

  return <div className="markdown">{blocks}</div>;
}
