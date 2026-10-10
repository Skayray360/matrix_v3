/* Creado por Aldo Garcia. */
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPage } from "../src/pages/ChatPage";
import {
  api,
  ApiError,
  type ChatReply,
  type ChatRequestStatus,
  type ConversationDetail,
  type DocumentStatus,
  type Me,
} from "../src/services/api";

const me: Me = {
  user_id: "synthetic",
  username: "test",
  display_name: "Test",
  auth_source: "local_test",
  roles: [],
  permissions: [],
  allowed_categories: [],
  category_wildcard: false,
  csrf_token: "test",
};
const detail = (id: string): ConversationDetail => ({
  id,
  title: `Conversacion ${id}`,
  created_at: "2026-09-09",
  updated_at: "2026-09-09",
  messages: [],
  attachments: [],
});
const capacityNotice =
  "Tu solicitud está en proceso. Puede demorar un poco porque tenemos muchas solicitudes en curso.";
const reply: ChatReply = {
  conversation_id: "A",
  message_id: "reply",
  answer: "Respuesta autorizada",
  sources: [],
  intent: "general",
  grounded: false,
  latency_ms: 1,
};
function status(overrides: Partial<ChatRequestStatus> = {}): ChatRequestStatus {
  return {
    status: "running",
    conversation_id: "A",
    response: null,
    capacity: {
      active: 1,
      limit: 50,
      queued: 0,
      queue_limit: 100,
      utilization_pct: 2,
      overloaded: false,
    },
    notice: null,
    poll_after_ms: 2000,
    ...overrides,
  };
}
function completed(response: ChatReply): ChatRequestStatus {
  return status({ status: "completed", conversation_id: response.conversation_id, response });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.spyOn(api, "listConversations").mockResolvedValue([detail("A"), detail("B")]);
  vi.spyOn(api, "getConversation").mockImplementation(async (id) => detail(id));
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ChatPage origen de respuestas", () => {
  const notice = "Respuesta general: no basada en documentación de la empresa";

  it("conserva el intent de una respuesta recién recibida", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(completed(reply));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Pregunta general{Enter}");
    expect(await screen.findByText(notice)).toBeInTheDocument();
  });

  it("conserva el aviso al reabrir una conversación almacenada", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getConversation).mockResolvedValue({
      ...detail("A"),
      messages: [
        {
          id: "general-history",
          role: "assistant",
          content: "Respuesta almacenada",
          intent: "general",
          model: null,
          created_at: "2026-09-09",
          sources: [],
        },
      ],
    });
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    expect(await screen.findByText(notice)).toBeInTheDocument();
    expect(screen.getByText("Respuesta almacenada")).toBeInTheDocument();
  });

  it("muestra nombres legibles y mantiene IDs completos de las fuentes privadas del historial", async () => {
    const user = userEvent.setup();
    const privateId = "__private__/documento-123/curriculum.pdf#0";
    const pdfId = "configured-knowledge/especializadas/prestaciones/subcarpeta/politica.pdf#2";
    vi.mocked(api.getConversation).mockResolvedValue({
      ...detail("A"),
      messages: [
        {
          id: "sources-history",
          role: "assistant",
          model: null,
          created_at: "2026-10-05",
          content: `Respuesta [[${privateId}]] [[${pdfId}]]`,
          sources: [{ source_id: privateId }, { source_id: pdfId }],
        },
      ],
    });
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    expect(await screen.findByText("curriculum.pdf#0")).toBeInTheDocument();
    expect(screen.getByText("politica.pdf#2")).toBeInTheDocument();
    const citations = screen.getAllByTestId("citation");
    expect(citations[0]).toHaveAttribute("title", privateId);
    expect(citations[1]).toHaveAttribute("title", pdfId);
  });

  it("conserva el origen al cargar mensajes anteriores", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getConversation)
      .mockResolvedValueOnce({ ...detail("A"), next_before_seq: 20 })
      .mockResolvedValueOnce({
        ...detail("A"),
        messages: [
          {
            id: "older-general",
            role: "assistant",
            content: "Respuesta general anterior",
            intent: "general",
            model: null,
            created_at: "2026-09-09",
            sources: [],
          },
        ],
      });
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await user.click(await screen.findByRole("button", { name: "Cargar mensajes anteriores" }));
    expect(await screen.findByText(notice)).toBeInTheDocument();
    expect(api.getConversation).toHaveBeenLastCalledWith("A", undefined, 20);
  });

  it("conserva página y etiqueta autorizadas al reabrir y paginar el historial", async () => {
    const user = userEvent.setup();
    const source = {
      source_id: "synthetic/manual.pdf#2",
      filename: "manual.pdf",
      label: "Manual sintético",
      page_or_sheet: "p. 3",
      section: "Condiciones",
      category: "prestaciones",
      scope: "corporate",
    };
    const historical = {
      id: "documental-history",
      role: "assistant",
      content: `Respuesta almacenada [[${source.source_id}]]`,
      model: null,
      created_at: "2026-10-08",
      sources: [source],
    };
    vi.mocked(api.getConversation)
      .mockResolvedValueOnce({ ...detail("A"), messages: [historical], next_before_seq: 20 })
      .mockResolvedValueOnce({
        ...detail("A"),
        messages: [{ ...historical, id: "older-documental", content: "Respuesta anterior" }],
      });
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    expect(await screen.findByTestId("sources")).toHaveTextContent("Manual sintético");
    expect(screen.getByTestId("sources")).toHaveTextContent("p. 3");
    await user.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
    await screen.findByText("Respuesta anterior");
    for (const sources of screen.getAllByTestId("sources")) {
      expect(sources).toHaveTextContent("Manual sintético");
      expect(sources).toHaveTextContent("p. 3");
    }
  });

  it("recibe la procedencia explícita y la muestra también en trazabilidad", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(
      completed({ ...reply, intent: "documental", answer_basis: "general" }),
    );
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Consulta sin documento{Enter}");
    expect(await screen.findByText(notice)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mostrar u ocultar trazabilidad" }));
    expect(screen.getByText("Conocimiento general")).toBeInTheDocument();
  });

  it("conserva procedencia explícita en historial y páginas anteriores", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getConversation)
      .mockResolvedValueOnce({
        ...detail("A"),
        next_before_seq: 20,
        messages: [
          {
            id: "mixed-history",
            role: "assistant",
            content: "Respuesta mixta almacenada",
            intent: "documental",
            answer_basis: "mixed",
            model: null,
            created_at: "2026-10-02",
            sources: [],
          },
        ],
      })
      .mockResolvedValueOnce({
        ...detail("A"),
        messages: [
          {
            id: "older-general-basis",
            role: "assistant",
            content: "Orientación general anterior",
            intent: "documental",
            answer_basis: "general",
            model: null,
            created_at: "2026-10-01",
            sources: [],
          },
        ],
      });
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    expect(await screen.findByTestId("answer-basis-notice")).toHaveTextContent("Respuesta mixta");
    await user.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
    expect(await screen.findByText(notice)).toBeInTheDocument();
    expect(screen.getByText("Respuesta mixta almacenada")).toBeInTheDocument();
  });
});

describe("ChatPage fallos de generación", () => {
  it("no publica como respuesta un fallo de validación documental", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(
      status({ status: "failed", error_code: "answer_unverified" }),
    );
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Pregunta documental{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("no pudo validarse");
    expect(screen.getByRole("alert")).not.toHaveTextContent("Ollama");
    expect(screen.getByRole("alert")).not.toHaveTextContent("precisar el documento");
    expect(screen.getByRole("alert")).toHaveTextContent("comparta esta referencia con TI");
    expect(screen.getByTestId("composer-input")).toHaveValue("Pregunta documental");
    expect(screen.queryByTestId("message-assistant")).not.toBeInTheDocument();
  });

  it.each([
    ["timeout", "La solicitud agotó el tiempo disponible"],
    ["model_incomplete", "El modelo devolvió una respuesta incompleta"],
    ["inference_unavailable", "No se pudo obtener una respuesta del modelo local"],
    ["generation_failed", "No fue posible generar la respuesta"],
    [undefined, "No fue posible generar la respuesta"],
  ])("explica %s, conserva el borrador e identifica la solicitud", async (code, reason) => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(status({ status: "failed", error_code: code }));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Pregunta pendiente{Enter}");
    const requestId = vi.mocked(api.submitChat).mock.calls[0][2];
    expect(await screen.findByRole("alert")).toHaveTextContent(reason);
    expect(screen.getByRole("alert")).toHaveTextContent(
      code === "model_incomplete"
        ? "TI puede revisar el modelo"
        : "comparta esta referencia con TI",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(requestId);
    expect(screen.getByTestId("composer-input")).toHaveValue("Pregunta pendiente");
    expect(screen.getByTestId("send-button")).toBeEnabled();
    expect(screen.queryByTestId("message-assistant")).not.toBeInTheDocument();
  });

  it("un cambio de permisos conserva el borrador y pide revisar la sesión", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(
      status({ status: "failed", error_code: "forbidden" }),
    );
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Pregunta privada{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Revise su sesión y los permisos");
    expect(screen.getByRole("alert")).not.toHaveTextContent("Ollama");
    expect(screen.getByTestId("composer-input")).toHaveValue("Pregunta privada");
  });

  it("muestra la referencia de operación del servidor para correlacionar sus logs", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(
      status({ status: "failed", error_code: "timeout", operation_id: "operation-log-123" }),
    );
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Pregunta pendiente{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("operation-log-123");
    expect(screen.getByRole("alert")).not.toHaveTextContent(
      vi.mocked(api.submitChat).mock.calls[0][2],
    );
  });

  it("permite reintentar un fallo confirmado sin duplicar mensajes ni recuperar el fallo anterior", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat")
      .mockResolvedValueOnce(status())
      .mockResolvedValueOnce(completed(reply));
    vi.spyOn(api, "chatStatus").mockResolvedValue(
      status({ status: "failed", error_code: "timeout" }),
    );
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Pregunta pendiente{Enter}");
    await screen.findByRole("alert", {}, { timeout: 3500 });
    const firstId = vi.mocked(api.submitChat).mock.calls[0][2];
    await user.click(screen.getByTestId("send-button"));
    await screen.findByText(reply.answer);
    const secondId = vi.mocked(api.submitChat).mock.calls[1][2];
    expect(secondId).not.toBe(firstId);
    expect(api.chatStatus).toHaveBeenCalledTimes(1);
    expect(screen.getAllByTestId("message-user")).toHaveLength(1);
    expect(screen.getAllByTestId("message-assistant")).toHaveLength(1);
    expect(screen.getByTestId("composer-input")).toHaveValue("");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("ChatPage concurrencia", () => {
  it("retira contenido borrado y permite reintentar una limpieza de adjuntos pendiente", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listConversations)
      .mockResolvedValueOnce([detail("A"), detail("B")])
      .mockResolvedValue([detail("B")]);
    vi.mocked(api.getConversation).mockResolvedValue({
      ...detail("A"),
      messages: [
        {
          id: "private-deleted",
          role: "assistant",
          content: "Contenido ya eliminado",
          intent: "documental",
          model: null,
          created_at: "2026-09-09",
          sources: [],
        },
      ],
    });
    vi.spyOn(api, "deleteConversation")
      .mockRejectedValueOnce(
        new ApiError(
          "conversation_cleanup_pending",
          "Conversacion eliminada; la limpieza de sus adjuntos sigue pendiente. Reintente la eliminacion.",
          503,
          null,
        ),
      )
      .mockResolvedValueOnce({ ok: true });
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    expect(await screen.findByText("Contenido ya eliminado")).toBeInTheDocument();
    await user.click(screen.getByLabelText("Eliminar conversacion Conversacion A"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "limpieza de sus adjuntos sigue pendiente",
    );
    expect(screen.queryByText("Contenido ya eliminado")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Conversacion A")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Reintentar limpieza" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(api.deleteConversation).toHaveBeenCalledTimes(2);
  });

  it("conserva la conversación si DELETE falla antes de confirmar el borrado", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getConversation).mockResolvedValue({
      ...detail("A"),
      messages: [
        {
          id: "not-deleted",
          role: "assistant",
          content: "Contenido no borrado",
          intent: "documental",
          model: null,
          created_at: "2026-09-09",
          sources: [],
        },
      ],
    });
    vi.spyOn(api, "deleteConversation").mockRejectedValue(new TypeError("Failed to fetch"));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await screen.findByText("Contenido no borrado");
    await user.click(screen.getByLabelText("Eliminar conversacion Conversacion A"));
    await screen.findByRole("alert");
    expect(screen.getByText("Contenido no borrado")).toBeInTheDocument();
  });

  it("una respuesta tardia de A no se muestra ni cambia la seleccion B", async () => {
    const user = userEvent.setup();
    const response = deferred<ChatReply>();
    vi.spyOn(api, "submitChat").mockImplementation(() => response.promise.then(completed));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "consulta A{Enter}");
    await waitFor(() => expect(api.submitChat).toHaveBeenCalled());
    await user.click(screen.getByText("Conversacion B"));
    await act(async () =>
      response.resolve({
        conversation_id: "A",
        message_id: "reply-A",
        answer: "Respuesta privada A",
        sources: [],
        intent: "general",
        grounded: false,
        latency_ms: 1,
      }),
    );
    expect(screen.queryByText("Respuesta privada A")).not.toBeInTheDocument();
    expect(screen.getByText("Conversacion B").closest("button")).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("descarta un historial obsoleto aunque el transporte ignore AbortSignal", async () => {
    const user = userEvent.setup();
    const first = deferred<ConversationDetail>();
    vi.mocked(api.getConversation).mockImplementation((id) =>
      id === "A" ? first.promise : Promise.resolve(detail(id)),
    );
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await user.click(screen.getByText("Conversacion B"));
    await act(async () =>
      first.resolve({
        ...detail("A"),
        messages: [
          {
            id: "old",
            role: "assistant",
            content: "Historial A",
            model: null,
            created_at: "2026-09-09",
            sources: [],
          },
        ],
      }),
    );
    expect(screen.queryByText("Historial A")).not.toBeInTheDocument();
  });

  it("conserva el borrador cuando falla la red", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockRejectedValue(new TypeError("Failed to fetch"));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Mi borrador{Enter}");
    await screen.findByRole("alert");
    expect(screen.getByTestId("composer-input")).toHaveValue("Mi borrador");
  });

  it("recupera el mismo envio incierto de A despues de consultar B", async () => {
    const user = userEvent.setup();
    const replyA: ChatReply = {
      conversation_id: "A",
      message_id: "reply-A",
      answer: "Respuesta recuperada A",
      sources: [],
      intent: "general",
      grounded: false,
      latency_ms: 1,
    };
    let recovered = false;
    vi.spyOn(api, "submitChat")
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(
        completed({
          ...replyA,
          conversation_id: "B",
          message_id: "reply-B",
          answer: "Respuesta B",
        }),
      );
    vi.spyOn(api, "chatStatus").mockImplementation(async () => {
      recovered = true;
      return completed(replyA);
    });
    vi.mocked(api.getConversation).mockImplementation(async (id) =>
      recovered && id === "A"
        ? {
            ...detail(id),
            messages: [
              {
                id: "reply-A",
                role: "assistant",
                content: replyA.answer,
                model: null,
                created_at: "2026-09-10",
                sources: [],
              },
            ],
          }
        : detail(id),
    );
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Consulta A{Enter}");
    await screen.findByRole("alert");
    const requestId = vi.mocked(api.submitChat).mock.calls[0][2];
    await user.click(screen.getByText("Conversacion B"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Consulta B{Enter}");
    await screen.findByText("Respuesta B");
    await user.click(screen.getByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    expect(screen.getByTestId("composer-input")).toHaveValue("Consulta A");
    await user.click(screen.getByTestId("send-button"));
    await screen.findByText("Respuesta recuperada A");
    expect(api.chatStatus).toHaveBeenCalledWith(requestId, expect.any(AbortSignal));
    expect(api.submitChat).toHaveBeenCalledTimes(2);
  });

  it("reconcilia la respuesta al regresar a A antes de que termine", async () => {
    const user = userEvent.setup();
    const response = deferred<ChatReply>();
    vi.spyOn(api, "submitChat").mockImplementation(() => response.promise.then(completed));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Consulta A{Enter}");
    await user.click(screen.getByText("Conversacion B"));
    await user.click(screen.getByText("Conversacion A"));
    await waitFor(() => expect(api.getConversation).toHaveBeenCalledTimes(3));
    vi.mocked(api.getConversation).mockResolvedValue({
      ...detail("A"),
      messages: [
        {
          id: "user-A",
          role: "user",
          content: "Consulta A",
          model: null,
          created_at: "2026-09-10",
          sources: [],
        },
        {
          id: "reply-A",
          role: "assistant",
          content: "Respuesta final A",
          model: null,
          created_at: "2026-09-10",
          sources: [],
        },
      ],
    });
    await act(async () =>
      response.resolve({
        conversation_id: "A",
        message_id: "reply-A",
        answer: "Respuesta final A",
        sources: [],
        intent: "general",
        grounded: false,
        latency_ms: 1,
      }),
    );
    expect(await screen.findByText("Respuesta final A")).toBeInTheDocument();
    expect(screen.getAllByTestId("message-user")).toHaveLength(1);
    expect(screen.getByText("Conversacion A").closest("button")).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("una lista tardia no restaura una conversacion eliminada", async () => {
    const user = userEvent.setup();
    const staleList = deferred<ConversationDetail[]>();
    vi.mocked(api.listConversations)
      .mockResolvedValueOnce([detail("A"), detail("B")])
      .mockReturnValueOnce(staleList.promise)
      .mockResolvedValueOnce([detail("A")]);
    vi.spyOn(api, "deleteConversation").mockResolvedValue({ ok: true });
    vi.spyOn(api, "submitChat").mockResolvedValue(
      completed({
        conversation_id: "A",
        message_id: "reply-A",
        answer: "Respuesta A",
        sources: [],
        intent: "general",
        grounded: false,
        latency_ms: 1,
      }),
    );
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Consulta A{Enter}");
    await waitFor(() => expect(api.listConversations).toHaveBeenCalledTimes(2));
    await user.click(screen.getByLabelText("Eliminar conversacion Conversacion B"));
    await waitFor(() => expect(screen.queryByText("Conversacion B")).not.toBeInTheDocument());
    await act(async () => staleList.resolve([detail("A"), detail("B")]));
    expect(screen.queryByText("Conversacion B")).not.toBeInTheDocument();
  });

  it("bloquea la consulta mientras el adjunto se carga e indexa", async () => {
    const user = userEvent.setup();
    const upload = deferred<{ documents: [] }>();
    vi.spyOn(api, "uploadAttachments").mockReturnValue(upload.promise);
    vi.spyOn(api, "submitChat");
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Resume el adjunto");
    await user.upload(
      screen.getByLabelText("Adjuntar archivos a la conversacion"),
      new File(["contenido"], "manual.txt"),
    );
    expect(screen.getByTestId("composer-input")).toBeDisabled();
    expect(screen.getByTestId("send-button")).toBeDisabled();
    expect(api.submitChat).not.toHaveBeenCalled();
    await act(async () => upload.resolve({ documents: [] }));
    expect(screen.getByTestId("composer-input")).toBeEnabled();
    expect(screen.getByTestId("composer-input")).toHaveValue("Resume el adjunto");
  });

  it("conserva la pregunta escrita al crear una conversación para el primer adjunto", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "createConversation").mockResolvedValue(detail("C"));
    vi.spyOn(api, "uploadAttachments").mockResolvedValue({ documents: [] });
    vi.spyOn(api, "submitChat").mockResolvedValue(completed({ ...reply, conversation_id: "C" }));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await screen.findByText("Conversacion A");
    await user.type(screen.getByTestId("composer-input"), "Resume mi adjunto");
    await user.upload(
      screen.getByLabelText("Adjuntar archivos a la conversacion"),
      new File(["contenido sintético"], "manual.txt"),
    );
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    expect(screen.getByTestId("composer-input")).toHaveValue("Resume mi adjunto");
    await user.click(screen.getByTestId("send-button"));
    await screen.findByText(reply.answer);
    expect(api.submitChat).toHaveBeenCalledWith(
      "Resume mi adjunto",
      "C",
      expect.any(String),
      expect.any(AbortSignal),
    );
  });

  it.each(["A", null])("un acceso rapido conserva el borrador en conversacion %s", async (id) => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(
      completed({
        conversation_id: "A",
        message_id: "quick-reply",
        answer: "Respuesta del tema",
        sources: [],
        intent: "general",
        grounded: false,
        latency_ms: 1,
      }),
    );
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await screen.findByText("Conversacion A");
    if (id) await user.click(screen.getByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Borrador pendiente");
    await user.click(screen.getByTestId("quick-action-general"));
    await screen.findByText("Respuesta del tema");
    expect(screen.getByTestId("composer-input")).toHaveValue("Borrador pendiente");
  });
});

describe("ChatPage ciclo de adjuntos", () => {
  const attachment: DocumentStatus = {
    id: "guide-A",
    filename: "Guia_configuracion_Kerberos_AD_PENOLEST_MX.docx",
    status: "indexed",
    chunk_count: 85,
    scope: "conversation",
    category: null,
    error_message: null,
  };

  it.each([
    "explicame sobre este documento Guia_configuracion_Kerberos_AD_PENOLEST_MX.",
    "Resume este documento",
  ])("mantiene el adjunto de A al enviar la consulta: %s", async (question) => {
    const user = userEvent.setup();
    vi.spyOn(api, "uploadAttachments").mockResolvedValue({ documents: [attachment] });
    vi.spyOn(api, "submitChat").mockResolvedValue(completed(reply));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.upload(
      screen.getByLabelText("Adjuntar archivos a la conversacion"),
      new File(["Contenido sintético"], attachment.filename),
    );
    await waitFor(() =>
      expect(screen.getByTestId("attachments")).toHaveTextContent("85 fragmentos"),
    );
    await user.type(screen.getByTestId("composer-input"), `${question}{Enter}`);
    await screen.findByText(reply.answer);
    expect(api.uploadAttachments).toHaveBeenCalledWith("A", [expect.any(File)]);
    expect(api.submitChat).toHaveBeenCalledWith(
      question,
      "A",
      expect.any(String),
      expect.any(AbortSignal),
    );
    expect(screen.getByTestId("attachments")).toHaveTextContent(attachment.filename);
    expect(screen.getByTestId("attachments")).toHaveTextContent("indexed");
  });

  it("reconcilia el estado del adjunto al regresar a A durante su carga", async () => {
    const user = userEvent.setup();
    const upload = deferred<{ documents: DocumentStatus[] }>();
    vi.spyOn(api, "uploadAttachments").mockReturnValue(upload.promise);
    vi.mocked(api.getConversation)
      .mockResolvedValueOnce(detail("A"))
      .mockResolvedValueOnce(detail("B"))
      .mockResolvedValueOnce({
        ...detail("A"),
        attachments: [{ ...attachment, status: "processing", chunk_count: 0 }],
      })
      .mockResolvedValue({ ...detail("A"), attachments: [attachment] });
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), "Resume el adjunto");
    await user.upload(
      screen.getByLabelText("Adjuntar archivos a la conversacion"),
      new File(["Contenido sintético"], attachment.filename),
    );
    await user.click(screen.getByText("Conversacion B"));
    expect(screen.queryByTestId("attachments")).not.toBeInTheDocument();
    await user.click(screen.getByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("attachments")).toHaveTextContent("processing"));
    await act(async () => upload.resolve({ documents: [attachment] }));
    await waitFor(() => expect(screen.getByTestId("attachments")).toHaveTextContent("indexed"));
    expect(screen.getByTestId("attachments")).toHaveTextContent("85 fragmentos");
    expect(screen.getAllByText(attachment.filename)).toHaveLength(1);
    expect(screen.getByTestId("composer-input")).toHaveValue("Resume el adjunto");
    expect(screen.getByTestId("composer-input")).toBeEnabled();
  });

  it("una carga tardía de A no añade sus archivos a B", async () => {
    const user = userEvent.setup();
    const upload = deferred<{ documents: DocumentStatus[] }>();
    vi.spyOn(api, "uploadAttachments").mockReturnValue(upload.promise);
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.upload(
      screen.getByLabelText("Adjuntar archivos a la conversacion"),
      new File(["Contenido sintético"], attachment.filename),
    );
    await user.click(screen.getByText("Conversacion B"));
    await act(async () => upload.resolve({ documents: [attachment] }));
    expect(screen.queryByTestId("attachments")).not.toBeInTheDocument();
    expect(screen.getByText("Conversacion B").closest("button")).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("un historial iniciado antes de terminar la carga no restaura el estado anterior", async () => {
    const user = userEvent.setup();
    const upload = deferred<{ documents: DocumentStatus[] }>();
    const history = deferred<ConversationDetail>();
    vi.spyOn(api, "uploadAttachments").mockReturnValue(upload.promise);
    vi.mocked(api.getConversation)
      .mockResolvedValueOnce(detail("A"))
      .mockResolvedValueOnce(detail("B"))
      .mockReturnValueOnce(history.promise);
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.upload(
      screen.getByLabelText("Adjuntar archivos a la conversacion"),
      new File(["Contenido sintético"], attachment.filename),
    );
    await user.click(screen.getByText("Conversacion B"));
    await user.click(screen.getByText("Conversacion A"));
    await act(async () => upload.resolve({ documents: [attachment] }));
    await act(async () =>
      history.resolve({
        ...detail("A"),
        attachments: [{ ...attachment, status: "processing", chunk_count: 0 }],
      }),
    );
    expect(screen.getByTestId("attachments")).toHaveTextContent("indexed");
    expect(screen.getByTestId("attachments")).toHaveTextContent("85 fragmentos");
    expect(screen.getAllByText(attachment.filename)).toHaveLength(1);
    expect(api.getConversation).toHaveBeenCalledTimes(3);
  });

  it("terminar una carga no cancela la navegación a una conversación nueva", async () => {
    const user = userEvent.setup();
    const upload = deferred<{ documents: DocumentStatus[] }>();
    const creation = deferred<ConversationDetail>();
    vi.spyOn(api, "uploadAttachments").mockReturnValue(upload.promise);
    vi.spyOn(api, "createConversation").mockReturnValue(creation.promise);
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.upload(
      screen.getByLabelText("Adjuntar archivos a la conversacion"),
      new File(["Contenido sintético"], attachment.filename),
    );
    await user.click(screen.getByTestId("new-conversation"));
    await act(async () => upload.resolve({ documents: [attachment] }));
    vi.mocked(api.listConversations).mockResolvedValue([detail("A"), detail("B"), detail("C")]);
    await act(async () => creation.resolve(detail("C")));
    expect((await screen.findByText("Conversacion C")).closest("button")).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(screen.queryByTestId("attachments")).not.toBeInTheDocument();
  });

  it("conserva la pregunta junto al primer adjunto aunque se navegue antes de crear su conversación", async () => {
    const user = userEvent.setup();
    const creation = deferred<ConversationDetail>();
    vi.spyOn(api, "createConversation").mockReturnValue(creation.promise);
    vi.spyOn(api, "uploadAttachments").mockResolvedValue({ documents: [attachment] });
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await screen.findByText("Conversacion A");
    await user.type(screen.getByTestId("composer-input"), "Pregunta sobre mi adjunto");
    await user.upload(
      screen.getByLabelText("Adjuntar archivos a la conversacion"),
      new File(["Contenido sintético"], attachment.filename),
    );
    await user.click(screen.getByText("Conversacion B"));
    vi.mocked(api.listConversations).mockResolvedValue([detail("A"), detail("B"), detail("C")]);
    await act(async () => creation.resolve(detail("C")));
    await screen.findByText("Conversacion C");
    expect(screen.getByText("Conversacion B").closest("button")).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(api.uploadAttachments).toHaveBeenCalledWith("C", [expect.any(File)]);
    vi.mocked(api.getConversation).mockResolvedValue({ ...detail("C"), attachments: [attachment] });
    await user.click(screen.getByText("Conversacion C"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    expect(screen.getByTestId("composer-input")).toHaveValue("Pregunta sobre mi adjunto");
    expect(screen.getByTestId("attachments")).toHaveTextContent(attachment.filename);
  });
});

describe("ChatPage admision y aviso de capacidad", () => {
  const saturated = status({
    status: "queued",
    notice: capacityNotice,
    capacity: {
      active: 50,
      limit: 50,
      queued: 1,
      queue_limit: 100,
      utilization_pct: 100,
      overloaded: true,
    },
  });

  async function send(
    user: ReturnType<typeof userEvent.setup>,
    message = "Consulta pendiente",
  ): Promise<void> {
    await user.click(await screen.findByText("Conversacion A"));
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled());
    await user.type(screen.getByTestId("composer-input"), `${message}{Enter}`);
  }

  it("no muestra saturacion bajo el umbral aunque el servidor incluya un texto de aviso", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(
      status({
        notice: capacityNotice,
        capacity: {
          active: 44,
          limit: 50,
          queued: 0,
          queue_limit: 100,
          utilization_pct: 88,
          overloaded: false,
        },
      }),
    );
    vi.spyOn(api, "chatStatus").mockResolvedValue(completed(reply));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await send(user);
    expect(screen.getByText("Procesando su consulta")).toBeInTheDocument();
    expect(screen.queryByTestId("capacity-notice")).not.toBeInTheDocument();
    expect(api.chatStatus).not.toHaveBeenCalled();
    expect(await screen.findByText(reply.answer, {}, { timeout: 3500 })).toBeInTheDocument();
    expect(api.submitChat).toHaveBeenCalledTimes(1);
  });

  it("muestra el aviso al 90 por ciento solo mientras la solicitud esta aceptada", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(
      status({
        notice: capacityNotice,
        capacity: {
          active: 45,
          limit: 50,
          queued: 0,
          queue_limit: 100,
          utilization_pct: 90,
          overloaded: true,
        },
      }),
    );
    vi.spyOn(api, "chatStatus").mockResolvedValue(completed(reply));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await send(user);
    expect(screen.getByTestId("capacity-notice")).toHaveAttribute("role", "status");
    expect(screen.getByTestId("capacity-notice")).toHaveTextContent(capacityNotice);
    await screen.findByText(reply.answer, {}, { timeout: 3500 });
    expect(screen.queryByTestId("capacity-notice")).not.toBeInTheDocument();
  });

  it("espera una aceptacion en cola y retira el aviso al bajar la ocupacion", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(saturated);
    vi.spyOn(api, "chatStatus")
      .mockResolvedValueOnce(status())
      .mockResolvedValueOnce(completed(reply));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await send(user);
    expect(screen.getByText("Solicitud aceptada, esperando disponibilidad")).toBeInTheDocument();
    expect(screen.getByTestId("capacity-notice")).toBeInTheDocument();
    await screen.findByText("Procesando su consulta", {}, { timeout: 3500 });
    expect(screen.queryByTestId("capacity-notice")).not.toBeInTheDocument();
    await screen.findByText(reply.answer, {}, { timeout: 3500 });
    expect(api.submitChat).toHaveBeenCalledTimes(1);
    expect(api.chatStatus).toHaveBeenCalledTimes(2);
  }, 7000);

  it("no afirma aceptacion ante 503 de cola llena y conserva el borrador", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockRejectedValue(
      new ApiError(
        "chat_capacity_full",
        "La cola está llena. Tu solicitud no fue aceptada.",
        503,
        null,
      ),
    );
    vi.spyOn(api, "chatStatus");
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await send(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("no fue aceptada");
    expect(screen.queryByTestId("capacity-notice")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Solicitud aceptada, esperando disponibilidad"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("composer-input")).toHaveValue("Consulta pendiente");
    expect(screen.getByTestId("composer-input")).toBeEnabled();
    expect(api.chatStatus).not.toHaveBeenCalled();
  });

  it("confirma cancelacion desde el estado sin borrar el borrador", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(saturated);
    vi.spyOn(api, "cancelChat").mockResolvedValue({ ok: true });
    vi.spyOn(api, "chatStatus").mockResolvedValue(status({ status: "cancelled" }));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await send(user);
    const requestId = vi.mocked(api.submitChat).mock.calls[0][2];
    await user.click(screen.getByRole("button", { name: "Cancelar solicitud" }));
    expect(api.cancelChat).toHaveBeenCalledWith(requestId);
    expect(screen.getByRole("button", { name: "Cancelación solicitada…" })).toBeDisabled();
    expect(await screen.findByRole("alert", {}, { timeout: 3500 })).toHaveTextContent(
      "Solicitud cancelada",
    );
    expect(screen.getByTestId("composer-input")).toHaveValue("Consulta pendiente");
    expect(screen.getByTestId("composer-input")).toBeEnabled();
    expect(screen.queryByTestId("capacity-notice")).not.toBeInTheDocument();
  });

  it.each([
    ["corte de red", new TypeError("Failed to fetch")],
    ["tiempo de HTTP agotado", new ApiError("request_timeout", "Tiempo agotado", 408, null)],
  ])("recupera el mismo identificador tras %s sin duplicar la pregunta", async (_name, failure) => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(saturated);
    vi.spyOn(api, "chatStatus")
      .mockRejectedValueOnce(failure)
      .mockResolvedValueOnce(completed(reply));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await send(user);
    const requestId = vi.mocked(api.submitChat).mock.calls[0][2];
    await screen.findByRole("alert", {}, { timeout: 3500 });
    expect(screen.queryByTestId("capacity-notice")).not.toBeInTheDocument();
    vi.mocked(api.getConversation).mockResolvedValue({
      ...detail("A"),
      messages: [
        {
          id: reply.message_id,
          role: "assistant",
          content: reply.answer,
          model: null,
          created_at: "2026-09-11",
          sources: [],
        },
      ],
    });
    await user.click(screen.getByTestId("send-button"));
    await screen.findByText(reply.answer);
    expect(api.submitChat).toHaveBeenCalledTimes(1);
    expect(api.chatStatus).toHaveBeenLastCalledWith(requestId, expect.any(AbortSignal));
  });

  it("no presenta el aviso ni la respuesta de A dentro de B", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(saturated);
    vi.spyOn(api, "chatStatus").mockResolvedValue(completed(reply));
    render(<ChatPage me={me} onLogout={vi.fn()} />);
    await send(user);
    expect(screen.getByTestId("capacity-notice")).toBeInTheDocument();
    await user.click(screen.getByText("Conversacion B"));
    expect(screen.queryByTestId("capacity-notice")).not.toBeInTheDocument();
    expect(
      screen.getByText("Hay una solicitud pendiente en otra conversación."),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("composer-input")).toBeEnabled(), {
      timeout: 3500,
    });
    expect(screen.queryByText(reply.answer)).not.toBeInTheDocument();
    expect(screen.getByText("Conversacion B").closest("button")).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("aborta el seguimiento y sus temporizadores al desmontar", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "submitChat").mockResolvedValue(saturated);
    vi.spyOn(api, "chatStatus").mockResolvedValue(completed(reply));
    const { unmount } = render(<ChatPage me={me} onLogout={vi.fn()} />);
    await send(user);
    const signal = vi.mocked(api.submitChat).mock.calls[0][3];
    unmount();
    expect(signal?.aborted).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 2100));
    expect(api.chatStatus).not.toHaveBeenCalled();
  });
});
