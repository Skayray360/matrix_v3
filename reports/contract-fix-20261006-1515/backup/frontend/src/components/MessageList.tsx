/* Creado por Aldo Garcia. */
/** Lista de mensajes de la conversacion con sus fuentes citadas. */

import { useEffect, useRef } from "react";

import { Icon } from "./Icon";
import { Mascot } from "./Mascot";
import { Markdown } from "../security/Markdown";
import type { AnswerBasis, SourceRef } from "../services/api";

export type DisplayMessage = {
  id: string;
  role: string;
  content: string;
  sources: SourceRef[];
  intent?: string | null;
  answer_basis?: AnswerBasis | null;
};

type Props = {
  messages: DisplayMessage[];
  pending: boolean;
  pendingLabel?: string;
  displayName: string;
};

export function MessageList({
  messages,
  pending,
  pendingLabel = "Consultando fuentes autorizadas",
  displayName,
}: Props): JSX.Element {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // Se desplaza al ultimo mensaje al llegar una respuesta.
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, pending]);

  if (messages.length === 0 && !pending) {
    return (
      <div className="messages" id="conversacion">
        <div className="welcome-state">
          <span className="welcome-eyebrow">
            <Icon name="chat" size={14} />
            Asistente documental
          </span>
          <h2>Consulta conocimiento interno con contexto</h2>
          <p>
            Hola, {displayName}. Consulte políticas y procesos de Recursos Humanos con las fuentes
            autorizadas para su cuenta.
          </p>
          <div className="welcome-grid">
            <div>
              <Icon name="general" size={20} />
              <strong>Base documental</strong>
              <span>Información corporativa disponible para su perfil.</span>
            </div>
            <div>
              <Icon name="paperclip" size={20} />
              <strong>Adjunto privado</strong>
              <span>Analice un archivo dentro de esta conversación.</span>
            </div>
            <div>
              <Icon name="shield" size={20} />
              <strong>Acceso por rol</strong>
              <span>Información general y permisos específicos de su cuenta.</span>
            </div>
          </div>
          <Mascot className="welcome-mascot" alt="Asistente IA Matrix" />
        </div>
      </div>
    );
  }

  return (
    <div className="messages" id="conversacion" aria-live="polite" aria-busy={pending}>
      <div className="thread">
        {messages.map((message) => {
          const known = new Set(message.sources.map((source) => source.source_id));
          // Los mensajes de entregas anteriores pueden no tener answer_basis.
          const basis = message.answer_basis ?? (message.intent === "general" ? "general" : null);
          const basisNotice =
            basis === "general"
              ? "Respuesta general: no basada en documentación de la empresa"
              : basis === "mixed"
                ? message.sources.length > 0
                  ? "Respuesta mixta: combina las fuentes citadas con conocimiento general del modelo"
                  : "Respuesta mixta: incluye conocimiento general; las fuentes documentales no están disponibles en esta respuesta"
                : basis === "insufficient"
                  ? "Documentación insuficiente para confirmar la información solicitada"
                  : null;
          // La cita en el texto pasa a ser el numero de la fuente en este
          // mensaje. Antes se imprimia el `source_id` completo entre corchetes
          // en mitad de la frase.
          const numbers = new Map(
            message.sources.map((source, index) => [source.source_id, index + 1] as const),
          );

          if (message.role === "user") {
            return (
              <article key={message.id} className="message user" data-testid="message-user">
                <div className="bubble">
                  <div className="message-role">Usted</div>
                  <Markdown text={message.content} knownSources={known} sourceNumbers={numbers} />
                </div>
              </article>
            );
          }

          return (
            <article
              key={message.id}
              className="message assistant"
              data-testid="message-assistant"
              data-message-id={message.id}
            >
              <span className="message-mark" aria-hidden="true">
                <Mascot />
              </span>
              <div className="message-body">
                <div className="message-head">
                  <span className="message-role">Matrix RH</span>
                  {message.sources.length > 0 ? (
                    <span className="grounded-badge">
                      <Icon name="check" size={11} />
                      {message.sources.length === 1
                        ? "1 fuente citada"
                        : `${message.sources.length} fuentes citadas`}
                    </span>
                  ) : null}
                </div>

                {basisNotice ? (
                  <p
                    className="general-notice"
                    data-testid={basis === "general" ? "general-notice" : "answer-basis-notice"}
                  >
                    <Icon name="general" size={14} />
                    <span>{basisNotice}</span>
                  </p>
                ) : null}

                <Markdown text={message.content} knownSources={known} sourceNumbers={numbers} />

                {message.sources.length > 0 ? (
                  <div className="sources" data-testid="sources">
                    <h4>Fuentes</h4>
                    <ul>
                      {message.sources.map((source, index) => (
                        <li key={source.source_id}>
                          <span className="source-chip" title={source.source_id}>
                            <span className="n" aria-hidden="true">
                              {index + 1}
                            </span>
                            <span className="name">{source.label}</span>
                            <span className="category">{source.category}</span>
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            </article>
          );
        })}

        {pending ? (
          <article className="message assistant" data-testid="message-pending">
            <span className="message-mark" aria-hidden="true">
              <Mascot />
            </span>
            <div className="message-body">
              <div className="message-head">
                <span className="message-role">Matrix RH</span>
              </div>
              <div className="typing-indicator" role="status">
                <span aria-hidden="true" />
                <span aria-hidden="true" />
                <span aria-hidden="true" />
                <p>{pendingLabel}</p>
              </div>
            </div>
          </article>
        ) : null}

        <div ref={endRef} />
      </div>
    </div>
  );
}
