/* Creado por Aldo Garcia. */
/** Barra lateral de conversaciones. Solo muestra conversaciones propias. */

import { useMemo, useState } from "react";

import { Icon } from "./Icon";
import { Mascot } from "./Mascot";
import { version } from "../../package.json";
import type { ConversationSummary } from "../services/api";

type Props = {
  open: boolean;
  conversations: ConversationSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  onClose: () => void;
};

/** Etiqueta corta de la ultima actividad. Ante una fecha ilegible, no inventa. */
function shortDate(iso: string): string {
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";

  const startOfDay = (date: Date): number =>
    new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((startOfDay(new Date()) - startOfDay(value)) / 86_400_000);

  if (days <= 0) return "hoy";
  if (days === 1) return "ayer";
  return value.toLocaleDateString("es-MX", { day: "numeric", month: "short" });
}

export function Sidebar({
  open,
  conversations,
  activeId,
  onSelect,
  onCreate,
  onDelete,
  onClose,
}: Props): JSX.Element {
  const [query, setQuery] = useState("");

  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("es");
    if (!needle) return conversations;
    return conversations.filter((conversation) =>
      conversation.title.toLocaleLowerCase("es").includes(needle),
    );
  }, [conversations, query]);

  return (
    <nav className="sidebar" id="conversaciones" data-open={open} aria-label="Conversaciones">
      <div className="sidebar-brand">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <Mascot />
          </span>
          <div className="brand-text">
            <strong>IA Matrix</strong>
            <span>Asistente local RAG</span>
          </div>
        </div>
        <button
          type="button"
          className="icon-button ghost sidebar-close"
          onClick={onClose}
          aria-label="Cerrar conversaciones"
        >
          <Icon name="close" />
        </button>
      </div>

      <div className="sidebar-actions">
        <button
          className="history-new-button"
          type="button"
          onClick={onCreate}
          data-testid="new-conversation"
        >
          <Icon name="plus" size={16} />
          Nueva conversacion
        </button>

        {conversations.length > 0 ? (
          <div className="history-search">
            <Icon name="search" size={16} />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Buscar en el historial"
              aria-label="Buscar en el historial"
              maxLength={120}
            />
          </div>
        ) : null}
      </div>

      <div className="sidebar-section-title">HISTORIAL</div>

      {conversations.length === 0 ? (
        <div className="empty-state">
          <Icon name="chat" size={22} />
          <strong>Aun no tiene conversaciones</strong>
          <p>Elija un tema o escriba su pregunta para empezar.</p>
        </div>
      ) : (
        <ul className="conversation-list" data-testid="conversation-list">
          {visible.map((conversation) => (
            <li
              key={conversation.id}
              className="conversation-item"
              aria-current={conversation.id === activeId}
            >
              <button
                type="button"
                className="select"
                aria-current={conversation.id === activeId ? "true" : undefined}
                onClick={() => onSelect(conversation.id)}
                title={conversation.title}
              >
                <span className="title">{conversation.title}</span>
                <span className="when">{shortDate(conversation.updated_at)}</span>
              </button>
              <button
                type="button"
                className="icon-button"
                onClick={() => onDelete(conversation.id)}
                aria-label={`Eliminar conversacion ${conversation.title}`}
              >
                <Icon name="trash" size={16} />
              </button>
            </li>
          ))}
        </ul>
      )}

      {conversations.length > 0 && visible.length === 0 ? (
        <p className="empty-state">Ninguna conversacion coincide con la busqueda.</p>
      ) : null}

      <div className="service-state">
        <Icon name="shield" />
        <div>
          <strong>Acceso por rol</strong>
          <small>Información según permisos de su cuenta</small>
        </div>
      </div>
      <div className="sidebar-footer">Matrix RH · interfaz {version}</div>
    </nav>
  );
}
