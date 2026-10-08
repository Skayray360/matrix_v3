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
import { api, ApiError, type Me } from "./services/api";

type SessionState = "checking" | "anonymous" | "authenticated";

export function App(): JSX.Element {
  const [state, setState] = useState<SessionState>("checking");
  const [me, setMe] = useState<Me | null>(null);

  const loadSession = useCallback(async () => {
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
      // Un fallo distinto de 401 (backend caido) tambien deja al usuario en la
      // pantalla de acceso, pero con el mensaje de error visible.
      setState("anonymous");
      setMe(null);
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
    return <LoginPage onAuthenticated={loadSession} />;
  }

  return <ChatPage key={me.user_id} me={me} onLogout={handleLogout} />;
}
