/* Creado por Aldo Garcia. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setCsrfToken } from "../src/services/api";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setCsrfToken("");
});

describe("transporte de solicitudes aceptadas", () => {
  it("rechaza una página HTML del proxy sin tratarla como respuesta de la API", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<!doctype html><html>Proxy</html>", {
          status: 200,
          headers: { "X-Request-ID": "synthetic-proxy" },
        }),
      ),
    );
    await expect(api.chatStatus("request-uncertain")).rejects.toMatchObject({
      code: "invalid_response",
      requestId: "synthetic-proxy",
    });
  });

  it("acepta 202 y envia el identificador, CSRF y AbortSignal al endpoint de cola", async () => {
    const accepted = {
      status: "queued",
      conversation_id: "A",
      response: null,
      capacity: {
        active: 50,
        limit: 50,
        queued: 1,
        queue_limit: 100,
        utilization_pct: 100,
        overloaded: true,
      },
      notice: null,
      poll_after_ms: 2000,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(accepted), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("synthetic-csrf");
    const controller = new AbortController();
    await expect(
      api.submitChat("Consulta", "A", "client-request-123", controller.signal),
    ).resolves.toEqual(accepted);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/chat/submit",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        signal: expect.any(AbortSignal),
        headers: { "Content-Type": "application/json", "X-CSRF-Token": "synthetic-csrf" },
        body: JSON.stringify({
          message: "Consulta",
          conversation_id: "A",
          client_request_id: "client-request-123",
        }),
      }),
    );
  });

  it("libera una consulta de estado que no responde sin cancelar la operación del servidor", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      (_url: string, options: RequestInit) =>
        new Promise((_resolve, reject) => {
          options.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = expect(api.chatStatus("request-uncertain")).rejects.toMatchObject({
      name: "ApiError",
      code: "request_timeout",
      status: 408,
    });
    await vi.advanceTimersByTimeAsync(30_000);
    await result;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/chat/requests/request-uncertain");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("propaga la cancelación del consumidor y limpia el temporizador", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, options: RequestInit) =>
          new Promise((_resolve, reject) => {
            options.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    const controller = new AbortController();
    const result = expect(
      api.chatStatus("cancelled-read", controller.signal),
    ).rejects.toMatchObject({
      name: "AbortError",
    });
    controller.abort();
    await result;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("aplica el límite también cuando llegan cabeceras pero el cuerpo queda pendiente", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, options: RequestInit) => ({
        status: 200,
        ok: true,
        headers: new Headers({ "X-Request-ID": "body-timeout" }),
        text: () =>
          new Promise((_resolve, reject) => {
            options.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      })),
    );
    const result = expect(api.chatStatus("slow-body")).rejects.toMatchObject({
      code: "request_timeout",
      requestId: "body-timeout",
    });
    await vi.advanceTimersByTimeAsync(30_000);
    await result;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("propaga un rechazo de capacidad como error y no como aceptacion", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "chat_capacity_full",
            message: "La solicitud no fue aceptada.",
          }),
          { status: 503, headers: { "X-Request-ID": "trace-123" } },
        ),
      ),
    );
    await expect(api.submitChat("Consulta", "A", "client-request-123")).rejects.toMatchObject({
      name: "ApiError",
      code: "chat_capacity_full",
      status: 503,
      requestId: "trace-123",
    } satisfies Partial<ApiError>);
  });
});
