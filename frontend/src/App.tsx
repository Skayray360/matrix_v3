/* Creado por Aldo Garcia. */
/**
 * Raiz de la aplicacion.
 *
 * La sesion se resuelve consultando `/me` contra la cookie HttpOnly. El
 * frontend NO decide si el usuario esta autenticado ni que puede ver: solo
 * refleja lo que el backend responde. Los roles que llegan en `/me` se usan
 * unicamente para etiquetas informativas.
 */

import { useCallback, useEffect, useState } from "react";

import { ChatPage } from "./pages/ChatPage";
import { LoginPage } from "./pages/LoginPage";
import { api, ApiError, setCsrfToken, type Me } from "./services/api";

type SessionState = "checking" | "anonymous" | "authenticated" | "unavailable";

export function App(): JSX.Element {
  const [state, setState] = useState<SessionState>("checking");
  const [me, setMe] = useState<Me | null>(null);
  const [sessionError, setSessionError] = useState("");

  const loadSession = useCallback(async () => {
    setState("checking");
    setSessionError("");
    try {
      const profile = await api.me();
      setMe(profile);
      setState("authenticated");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setState("anonymous");
        setMe(null);
        return;
      }
      // Un fallo de transporte/servidor no demuestra que la sesion haya
      // vencido. Permite consultar de nuevo sin pedir otra vez credenciales.
      setState("unavailable");
      setMe(null);
      setSessionError(
        error instanceof ApiError
          ? `${error.message}${error.requestId ? ` (referencia: ${error.requestId})` : ""}`
          : "No fue posible contactar al servidor de Matrix RH. Compruebe la conexión e inténtelo nuevamente.",
      );
    }
  }, []);

  useEffect(() => {
    void loadSession();
  }, [loadSession]);

  const handleLogout = useCallback(async () => {
    try {
      await api.logout();
    } catch (caught) {
      // Una sesion ya vencida equivale a cerrada. Un fallo de red no confirma
      // revocacion: mantener la pantalla y permitir reintentar desde el chat.
      if (!(caught instanceof ApiError && caught.status === 401)) throw caught;
    }
    setMe(null);
    setCsrfToken("");
    setSessionError("");
    setState("anonymous");
  }, []);

  const handleSessionExpired = useCallback(() => {
    setCsrfToken("");
    setMe(null);
    setSessionError("Su sesión expiró. Inicie sesión nuevamente.");
    setState("anonymous");
  }, []);

  if (state === "checking") {
    return (
      <div className="app-loading">
        <p className="loading-dots" role="status">
          Verificando sesion
        </p>
      </div>
    );
  }

  if (state === "anonymous" || !me) {
    if (state === "unavailable") {
      return (
        <main className="recovery-shell">
          <section className="recovery-card" role="alert">
            <h1>No se pudo verificar su sesión</h1>
            <p>{sessionError}</p>
            <button className="secondary-button" type="button" onClick={() => void loadSession()}>
              Reintentar conexión
            </button>
          </section>
        </main>
      );
    }
    return <LoginPage onAuthenticated={loadSession} notice={sessionError} />;
  }

  return (
    <ChatPage
      key={me.user_id}
      me={me}
      onLogout={handleLogout}
      onProfileUpdated={setMe}
      onSessionExpired={handleSessionExpired}
    />
  );
}
