/* Creado por Aldo Garcia. */
/**
 * Pantalla principal de chat.
 *
 * Punto importante del diseno: `Matrix` y `MatrixR1` ven **exactamente la misma
 * interfaz**. No hay ninguna opcion oculta por rol. La diferencia de informacion
 * proviene unicamente del backend y sus politicas; ocultar un boton nunca es un
 * control de seguridad.
 */

import { useCallback, useEffect, useState, useRef } from "react";

import { ChangePasswordDialog } from "../components/ChangePasswordDialog";
import { Composer } from "../components/Composer";
import { Icon } from "../components/Icon";
import { Mascot } from "../components/Mascot";
import { MessageList, type DisplayMessage } from "../components/MessageList";
import { QuickActions } from "../components/QuickActions";
import { Sidebar } from "../components/Sidebar";
import { ThemeControl } from "../components/ThemeControl";
import { TracePanel, type ExecutionTrace } from "../components/TracePanel";
import {
  api,
  ApiError,
  type ChatMessage,
  type ChatRequestStatus,
  type ConversationSummary,
  type DocumentStatus,
  type Me,
  type SourceRef,
} from "../services/api";

type Props = {
  me: Me;
  onLogout: () => void | Promise<void>;
  onProfileUpdated?: (profile: Me) => void;
  onSessionExpired?: () => void;
};

const CAPACITY_NOTICE =
  "Tu solicitud está en proceso. Puede demorar un poco porque tenemos muchas solicitudes en curso.";

function generationFailure(code: ChatRequestStatus["error_code"], requestId: string): string {
  if (code === "forbidden") {
    return `No tiene permiso para completar esta solicitud. El borrador se conservó. Revise su sesión y los permisos antes de reintentar. Referencia de la solicitud: ${requestId}`;
  }
  if (code === "answer_unverified") {
    return `La respuesta generada no pudo validarse con las fuentes autorizadas y no se publicó. El borrador se conservó. Puede reintentar la consulta. Si se repite, comparta esta referencia con TI. Referencia de la solicitud: ${requestId}`;
  }
  const reason =
    code === "timeout"
      ? "La solicitud agotó el tiempo disponible."
      : code === "model_incomplete"
        ? "El modelo devolvió una respuesta incompleta."
        : code === "inference_unavailable"
          ? "No se pudo obtener una respuesta del modelo local."
          : "No fue posible generar la respuesta.";
  const nextStep =
    code === "model_incomplete"
      ? "Si se repite, TI puede revisar el modelo y el límite de generación con esta referencia."
      : "Vuelva a intentarlo. Si continúa, comparta esta referencia con TI para revisar el servicio local.";
  return `${reason} El borrador se conservó. ${nextStep} Referencia de la solicitud: ${requestId}`;
}

/** Cancela tambien la espera entre consultas: desmontar no deja timers vivos. */
function waitForStatus(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const cancel = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(
      () => {
        signal.removeEventListener("abort", cancel);
        resolve();
      },
      Math.min(10000, Math.max(2000, milliseconds || 2000)),
    );
    signal.addEventListener("abort", cancel, { once: true });
  });
}

/** Iniciales para el avatar de identidad. Nunca mas de dos letras. */
function initials(displayName: string): string {
  const parts = displayName.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toLocaleUpperCase("es");
  return `${parts[0][0]}${parts[parts.length - 1][0]}`.toLocaleUpperCase("es");
}

/** Conserva los metadatos autorizados; un historial antiguo puede tener solo IDs. */
function historySource(source: ChatMessage["sources"][number]): SourceRef {
  const sourceId = source.source_id;
  const parts = sourceId.split("/");
  const filename = source.filename || parts[parts.length - 1] || sourceId;
  return {
    source_id: sourceId,
    category: source.category ?? "",
    filename,
    section: source.section ?? "",
    page_or_sheet: source.page_or_sheet ?? "",
    score: source.score ?? 0,
    label: source.label || filename,
    scope: source.scope ?? (sourceId.startsWith("__private__/") ? "conversation" : "corporate"),
  };
}

function mergeAttachments(current: DocumentStatus[], incoming: DocumentStatus[]): DocumentStatus[] {
  const replacedIds = new Set(incoming.map((item) => item.id));
  return [...current.filter((item) => !replacedIds.has(item.id)), ...incoming];
}

export function ChatPage({ me, onLogout, onProfileUpdated, onSessionExpired }: Props): JSX.Element {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [attachments, setAttachments] = useState<DocumentStatus[]>([]);
  const [pending, setPending] = useState(false);
  const [requestStatus, setRequestStatus] = useState<ChatRequestStatus | null>(null);
  const [requestConversation, setRequestConversation] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingCleanups, setPendingCleanups] = useState<{ id: string; message: string }[]>([]);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [passwordDialogOpen, setPasswordDialogOpen] = useState(false);
  const [trace, setTrace] = useState<ExecutionTrace | null>(null);
  const [uploading, setUploading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [beforeSeq, setBeforeSeq] = useState<number | null>(null);
  const selected = useRef<string | null>(null);
  const navigation = useRef(0);
  const conversationListRequest = useRef(0);
  const mounted = useRef(false);
  const readController = useRef<AbortController | null>(null);
  const sending = useRef(false);
  const uploadingRef = useRef(false);
  const activeRequest = useRef<string | null>(null);
  const requestController = useRef<AbortController | null>(null);
  const uncertain = useRef(new Map<string, string>());
  const completedUpload = useRef<{
    conversationId: string;
    documents: DocumentStatus[];
  } | null>(null);

  useEffect(() => {
    const uncertainRequests = uncertain.current;
    mounted.current = true;
    return () => {
      mounted.current = false;
      navigation.current += 1;
      conversationListRequest.current += 1;
      readController.current?.abort();
      requestController.current?.abort();
      uncertainRequests.clear();
    };
  }, []);

  const describeError = useCallback((caught: unknown): string => {
    if (caught instanceof ApiError) {
      return caught.requestId
        ? `${caught.message} (referencia: ${caught.requestId})`
        : caught.message;
    }
    return "No fue posible completar la operacion.";
  }, []);

  const logout = useCallback(async () => {
    try {
      await onLogout();
    } catch (caught) {
      if (mounted.current)
        setError(
          `No se pudo confirmar el cierre de sesión. Inténtelo nuevamente. ${describeError(caught)}`,
        );
    }
  }, [onLogout, describeError]);

  const refreshConversations = useCallback(async () => {
    const ticket = ++conversationListRequest.current;
    try {
      const result = await api.listConversations();
      if (mounted.current && ticket === conversationListRequest.current) setConversations(result);
    } catch (caught) {
      if (mounted.current && ticket === conversationListRequest.current)
        setError(describeError(caught));
    }
  }, [describeError]);

  useEffect(() => {
    void refreshConversations();
  }, [refreshConversations]);

  const openConversation = useCallback(
    async (id: string) => {
      const ticket = ++navigation.current;
      const uploadAtStart = completedUpload.current;
      selected.current = id;
      setActiveId(id);
      setMessages([]);
      setAttachments([]);
      setTrace(null);
      setBeforeSeq(null);
      setLoading(true);
      setError(null);
      setSidebarOpen(false);
      readController.current?.abort();
      readController.current = new AbortController();
      try {
        const detail = await api.getConversation(id, readController.current.signal);
        if (ticket !== navigation.current) return;
        setBeforeSeq(detail.next_before_seq ?? null);
        const upload = completedUpload.current;
        // Una lectura iniciada antes de completar la carga puede conservar
        // un estado viejo; solo combinar el resultado más reciente de ese chat.
        setAttachments(
          upload && upload !== uploadAtStart && upload.conversationId === id
            ? mergeAttachments(detail.attachments, upload.documents)
            : detail.attachments,
        );
        // La traza corresponde exclusivamente a la ultima respuesta recibida
        // en la conversacion activa. No se reutiliza metadata de otra sesion.
        setTrace(null);
        setMessages(
          detail.messages.map((message) => ({
            id: message.id,
            role: message.role,
            content: message.content,
            intent: message.intent,
            answer_basis: message.answer_basis,
            sources: message.sources.map(historySource),
          })),
        );
      } catch (caught) {
        if (
          ticket === navigation.current &&
          !(caught instanceof DOMException && caught.name === "AbortError")
        ) {
          setError(describeError(caught));
        }
      } finally {
        if (ticket === navigation.current) setLoading(false);
      }
    },
    [describeError],
  );

  const startConversation = useCallback(async () => {
    const ticket = ++navigation.current;
    setError(null);
    setSidebarOpen(false);
    readController.current?.abort();
    setLoading(true);
    try {
      const created = await api.createConversation();
      if (ticket !== navigation.current) return;
      selected.current = created.id;
      setActiveId(created.id);
      setBeforeSeq(null);
      setMessages([]);
      setAttachments([]);
      setTrace(null);
      await refreshConversations();
    } catch (caught) {
      if (ticket === navigation.current) setError(describeError(caught));
    } finally {
      if (ticket === navigation.current) setLoading(false);
    }
  }, [describeError, refreshConversations]);

  const clearSelectedConversation = useCallback((id: string) => {
    if (id !== selected.current) return;
    navigation.current += 1;
    selected.current = null;
    setActiveId(null);
    setLoading(false);
    setBeforeSeq(null);
    readController.current?.abort();
    setMessages([]);
    setAttachments([]);
    setTrace(null);
  }, []);

  const removeConversation = useCallback(
    async (id: string) => {
      setCleanupBusy(true);
      try {
        await api.deleteConversation(id);
        clearSelectedConversation(id);
        setPendingCleanups((items) => items.filter((item) => item.id !== id));
        setError(null);
        await refreshConversations();
      } catch (caught) {
        if (caught instanceof ApiError && caught.code === "conversation_cleanup_pending") {
          // El servidor confirmo la baja SQL; no conservar sus mensajes en pantalla.
          clearSelectedConversation(id);
          setPendingCleanups((items) => [
            ...items.filter((item) => item.id !== id),
            {
              id,
              message: describeError(caught),
            },
          ]);
          setError(null);
          await refreshConversations();
        } else {
          setError(describeError(caught));
        }
      } finally {
        setCleanupBusy(false);
      }
    },
    [clearSelectedConversation, describeError, refreshConversations],
  );

  const sendMessage = useCallback(
    async (text: string): Promise<boolean> => {
      if (sending.current || uploadingRef.current || loading) return false;
      sending.current = true;
      const destination = selected.current;
      const ticket = navigation.current;
      // Cada envio incierto conserva su identificador incluso si se consulta
      // otra conversacion o se cambia el borrador antes de volver a intentarlo.
      const requestKey = JSON.stringify([destination, text]);
      const previous = uncertain.current.get(requestKey);
      const requestId = previous ?? crypto.randomUUID();
      activeRequest.current = requestId;
      const controller = new AbortController();
      requestController.current = controller;
      uncertain.current.set(requestKey, requestId);
      setError(null);
      setPending(true);
      setRequestStatus(null);
      setRequestConversation(destination);
      setCancelling(false);
      try {
        let status: ChatRequestStatus | undefined;
        if (previous) {
          try {
            status = await api.chatStatus(requestId, controller.signal);
          } catch (caught) {
            if (!(caught instanceof ApiError && caught.status === 404)) throw caught;
          }
        }
        status ??= await api.submitChat(text, destination, requestId, controller.signal);
        while (status.status === "queued" || status.status === "running") {
          if (controller.signal.aborted || !mounted.current) return false;
          setRequestStatus(status);
          await waitForStatus(status.poll_after_ms, controller.signal);
          status = await api.chatStatus(requestId, controller.signal);
        }
        if (controller.signal.aborted || !mounted.current) return false;
        if (status.status !== "completed" || !status.response) {
          uncertain.current.delete(requestKey);
          if (status.status === "cancelled")
            throw new Error("Solicitud cancelada. El borrador se conservó.");
          if (status.status === "expired")
            throw new Error(
              "La solicitud venció sin respuesta. El borrador se conservó; puede volver a enviarlo.",
            );
          throw new Error(generationFailure(status.error_code, status.operation_id ?? requestId));
        }
        const reply = status.response;
        uncertain.current.delete(requestKey);
        // La respuesta queda almacenada por el servidor en su conversacion.
        // Solo la presenta si sigue seleccionada; nunca cambia la navegacion.
        if (
          mounted.current &&
          selected.current === destination &&
          destination &&
          (navigation.current !== ticket || previous)
        ) {
          // Al regresar a A durante su respuesta, el historial abierto puede
          // preceder a la persistencia. Se reconcilia A sin seleccionar otra.
          await openConversation(destination);
        } else if (
          mounted.current &&
          navigation.current === ticket &&
          selected.current === destination
        ) {
          selected.current = reply.conversation_id;
          setActiveId(reply.conversation_id);
          setMessages((current) => [
            ...current,
            { id: `user-${requestId}`, role: "user", content: text, sources: [] },
            {
              id: reply.message_id,
              role: "assistant",
              content: reply.answer,
              sources: reply.sources,
              intent: reply.intent,
              answer_basis: reply.answer_basis,
            },
          ]);
          setTrace({
            conversationId: reply.conversation_id,
            intent: reply.intent,
            grounded: reply.grounded,
            answerBasis: reply.answer_basis,
            latencyMs: reply.latency_ms,
            sources: reply.sources,
          });
        }
        setDrafts((current) => {
          const key = destination ?? "new";
          if ((current[key] ?? "").trim() === text) return { ...current, [key]: "" };
          // Un acceso rapido envia su propia pregunta: conserva el borrador
          // del usuario, tambien si el servidor acaba de crear la conversacion.
          if (destination === null && current.new) {
            return { ...current, new: "", [reply.conversation_id]: current.new };
          }
          return current;
        });
        if (mounted.current) await refreshConversations();
        return true;
      } catch (caught) {
        if (caught instanceof ApiError && caught.code === "chat_capacity_full")
          uncertain.current.delete(requestKey);
        if (mounted.current && navigation.current === ticket && !controller.signal.aborted) {
          setError(
            caught instanceof TypeError ||
              (caught instanceof ApiError &&
                ["request_timeout", "invalid_response"].includes(caught.code))
              ? "No fue posible confirmar el resultado. Conservamos el borrador; vuelve a enviar el mismo mensaje para consultar su estado sin duplicarlo."
              : caught instanceof Error && !(caught instanceof ApiError)
                ? caught.message
                : describeError(caught),
          );
        }
        return false;
      } finally {
        sending.current = false;
        activeRequest.current = null;
        requestController.current = null;
        if (mounted.current) {
          setPending(false);
          setRequestStatus(null);
          setCancelling(false);
        }
      }
    },
    [describeError, refreshConversations, loading, openConversation],
  );

  const cancelRequest = useCallback(async () => {
    const requestId = activeRequest.current;
    if (!requestId || cancelling) return;
    setCancelling(true);
    try {
      await api.cancelChat(requestId);
      // El estado confirmado por el servidor decide el resultado. Cancelar
      // una ejecucion no significa que la GPU ya haya liberado sus recursos.
    } catch (caught) {
      if (mounted.current && activeRequest.current === requestId) {
        setError(describeError(caught));
        setCancelling(false);
      }
    }
  }, [cancelling, describeError]);

  const uploadFiles = useCallback(
    async (files: File[]) => {
      if (sending.current || uploadingRef.current || loading || !files.length) return;
      uploadingRef.current = true;
      setUploading(true);
      setError(null);
      const ticket = navigation.current;
      let conversationId = selected.current;
      const placeholders: DocumentStatus[] = files.map((file) => ({
        id: crypto.randomUUID(),
        filename: file.name,
        status: "uploading",
        chunk_count: 0,
        scope: "conversation",
        category: null,
        error_message: null,
      }));
      setAttachments((current) => [...current, ...placeholders]);
      try {
        if (!conversationId) {
          const created = await api.createConversation();
          conversationId = created.id;
          if (!mounted.current) return;
          // La pregunta pertenece al chat creado para su adjunto aunque el
          // usuario haya navegado mientras el servidor asignaba el ID.
          setDrafts((current) => ({
            ...current,
            new: "",
            [created.id]: current.new ?? "",
          }));
          if (navigation.current === ticket) {
            selected.current = created.id;
            setActiveId(created.id);
            setTrace(null);
          }
        }
        const result = await api.uploadAttachments(conversationId, files);
        if (!mounted.current) return;
        completedUpload.current = { conversationId, documents: result.documents };
        if (selected.current === conversationId) {
          const placeholderIds = new Set(placeholders.map((item) => item.id));
          setAttachments((current) =>
            mergeAttachments(
              current.filter((item) => !placeholderIds.has(item.id)),
              result.documents,
            ),
          );
        }
        if (mounted.current) await refreshConversations();
      } catch (caught) {
        if (
          mounted.current &&
          selected.current === conversationId &&
          (conversationId !== null || navigation.current === ticket)
        ) {
          setError(describeError(caught));
          setAttachments((current) =>
            current.map((item) =>
              placeholders.some((p) => p.id === item.id)
                ? { ...item, status: "failed", error_message: describeError(caught) }
                : item,
            ),
          );
        }
      } finally {
        uploadingRef.current = false;
        if (mounted.current) setUploading(false);
      }
    },
    [describeError, refreshConversations, loading],
  );

  async function loadOlder(): Promise<void> {
    if (!selected.current || !beforeSeq || loading) return;
    const destination = selected.current;
    const ticket = navigation.current;
    setLoading(true);
    try {
      const detail = await api.getConversation(destination, undefined, beforeSeq);
      if (ticket !== navigation.current) return;
      setBeforeSeq(detail.next_before_seq ?? null);
      setMessages((current) => [
        ...detail.messages.map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content,
          intent: m.intent,
          answer_basis: m.answer_basis,
          sources: m.sources.map(historySource),
        })),
        ...current,
      ]);
    } catch (caught) {
      if (ticket === navigation.current) setError(describeError(caught));
    } finally {
      if (ticket === navigation.current) setLoading(false);
    }
  }

  const cleanupRequest = pendingCleanups[0];
  const visibleError = error ?? cleanupRequest?.message;

  return (
    <div className="app-shell">
      <a className="skip-link" href="#conversacion">
        Ir a la conversacion
      </a>

      <Sidebar
        open={sidebarOpen}
        conversations={conversations}
        activeId={activeId}
        onSelect={openConversation}
        onCreate={startConversation}
        onDelete={removeConversation}
        onClose={() => setSidebarOpen(false)}
      />

      <header className="app-header">
        <div className="header-title-group">
          <button
            className="icon-button menu-toggle"
            type="button"
            onClick={() => setSidebarOpen((open) => !open)}
            aria-expanded={sidebarOpen}
            aria-controls="conversaciones"
            aria-label="Conversaciones"
            title="Conversaciones"
          >
            <Icon name="menu" />
          </button>
          <span className="header-mascot">
            <Mascot />
          </span>
          <div>
            <h1 className="app-title">IA Matrix</h1>
            <span className="app-subtitle">Consulta documental · Recursos Humanos</span>
          </div>
        </div>

        <div className="header-actions">
          <span className="local-badge">Sesión activa</span>

          <ThemeControl />

          <button
            className="toggle-button"
            type="button"
            onClick={() => setInspectorOpen((open) => !open)}
            aria-expanded={inspectorOpen}
            aria-label="Mostrar u ocultar trazabilidad"
            title="Trazabilidad"
          >
            <Icon name="panel" size={17} />
            <span className="label">Trazabilidad</span>
          </button>

          <span className="header-divider" aria-hidden="true" />

          <div className="identity">
            <span className="avatar" aria-hidden="true">
              {initials(me.display_name)}
            </span>
            <span className="identity-text">
              <span className="identity-name" data-testid="identity-user">
                {me.display_name}
              </span>
              <span className="identity-meta">
                <span data-testid="identity-source">{me.auth_source}</span>
                <span className="sep" aria-hidden="true">
                  ·
                </span>
                <span data-testid="identity-scope">
                  {me.category_wildcard
                    ? "acceso de negocio ampliado"
                    : `${me.allowed_categories.length} categoria(s)`}
                </span>
              </span>
            </span>
          </div>

          {me.auth_source === "local" ? (
            <button
              className="icon-button"
              type="button"
              aria-label="Cambiar contraseña"
              title="Cambiar contraseña"
              onClick={() => setPasswordDialogOpen(true)}
            >
              <Icon name="shield" />
            </button>
          ) : null}

          <button className="secondary-button" type="button" onClick={() => void logout()}>
            Salir
          </button>
        </div>
      </header>

      {/* El aviso ocupa su propia fila de la rejilla. Antes flotaba sobre la
          conversacion y tapaba el primer mensaje. */}
      {visibleError ? (
        <div className="banner" role="alert" data-testid="error-banner">
          <Icon name="alert" size={17} />
          <p>{visibleError}</p>
          {cleanupRequest ? (
            <button
              className="cleanup-retry"
              type="button"
              disabled={cleanupBusy}
              onClick={() => void removeConversation(cleanupRequest.id)}
            >
              {cleanupBusy ? "Completando limpieza…" : "Reintentar limpieza"}
            </button>
          ) : null}
          {error ? (
            <button type="button" onClick={() => setError(null)} aria-label="Cerrar aviso">
              <Icon name="close" size={16} />
            </button>
          ) : null}
        </div>
      ) : null}

      <QuickActions
        disabled={pending || uploading || loading}
        onSelect={(prompt) => void sendMessage(prompt)}
      />

      <main className={`main${inspectorOpen ? " inspector-open" : ""}`}>
        <section className="chat-surface" aria-label="Conversacion con Matrix RH">
          {loading ? <p role="status">Cargando conversacion…</p> : null}
          {uploading ? <p role="status">Cargando y procesando archivos…</p> : null}
          {beforeSeq ? (
            <button type="button" disabled={loading} onClick={() => void loadOlder()}>
              Cargar mensajes anteriores
            </button>
          ) : null}
          {pending &&
          requestConversation === activeId &&
          requestStatus?.capacity.overloaded &&
          (requestStatus.status === "queued" || requestStatus.status === "running") ? (
            <p className="capacity-notice" role="status" data-testid="capacity-notice">
              {CAPACITY_NOTICE}
            </p>
          ) : null}
          {pending && requestStatus ? (
            <button type="button" disabled={cancelling} onClick={() => void cancelRequest()}>
              {cancelling ? "Cancelación solicitada…" : "Cancelar solicitud"}
            </button>
          ) : null}
          {pending && requestConversation !== activeId ? (
            <p role="status">Hay una solicitud pendiente en otra conversación.</p>
          ) : null}
          <MessageList
            messages={messages}
            pending={pending && requestConversation === activeId}
            pendingLabel={
              requestStatus?.status === "queued"
                ? "Solicitud aceptada, esperando disponibilidad"
                : requestStatus?.status === "running"
                  ? "Procesando su consulta"
                  : "Enviando solicitud"
            }
            displayName={me.display_name}
          />

          <Composer
            disabled={pending || uploading || loading}
            attachments={attachments}
            draft={drafts[activeId ?? "new"] ?? ""}
            onDraftChange={(text) =>
              setDrafts((current) => ({ ...current, [activeId ?? "new"]: text }))
            }
            onSend={sendMessage}
            onUpload={(files) => void uploadFiles(files)}
          />
        </section>

        {inspectorOpen ? (
          <TracePanel trace={trace} onClose={() => setInspectorOpen(false)} />
        ) : null}
      </main>

      {sidebarOpen ? (
        <button
          className="scrim"
          type="button"
          aria-label="Cerrar conversaciones"
          onClick={() => setSidebarOpen(false)}
        />
      ) : null}

      {passwordDialogOpen && me.auth_source === "local" ? (
        <ChangePasswordDialog
          onChanged={(profile) => onProfileUpdated?.(profile)}
          onClose={() => setPasswordDialogOpen(false)}
          onSessionExpired={onSessionExpired}
        />
      ) : null}
    </div>
  );
}
