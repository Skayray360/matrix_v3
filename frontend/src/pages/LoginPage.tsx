/* Creado por Aldo Garcia. */
/**
 * Pantalla de acceso.
 *
 * El formulario local se usa con `AUTH_PROVIDER=local` o `local_test`. Cuando
 * el backend corre con OIDC o Entra ID, el boton corporativo redirige a
 * `/api/v1/auth/login` y el flujo OIDC ocurre entero en el servidor: el
 * navegador nunca ve un token.
 *
 * La referencia rag_local_Gemma aporta solo la apariencia; la autenticacion
 * y la autorizacion siguen perteneciendo al backend Matrix RH.
 *
 * El mensaje de error es siempre el que devuelve el backend, que no distingue
 * entre usuario inexistente y contrasena incorrecta.
 */

import { useState, type FormEvent } from "react";

import { Icon } from "../components/Icon";
import { Mascot } from "../components/Mascot";
import { version } from "../../package.json";
import { api, ApiError } from "../services/api";

type Props = {
  onAuthenticated: () => void | Promise<void>;
  notice?: string;
};

export function LoginPage({ onAuthenticated, notice }: Props): JSX.Element {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.localLogin(username, password);
      await onAuthenticated();
    } catch (caught) {
      const message =
        caught instanceof ApiError
          ? caught.message
          : "No fue posible contactar al servidor de Matrix RH.";
      setError(message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell" aria-label="Acceso a IA Matrix">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand">
          <span className="login-brand-mark">
            <Mascot />
          </span>
          <div>
            <p className="login-kicker">Plataforma empresarial</p>
            <h1 id="login-title">IA Matrix</h1>
          </div>
        </div>
        <p className="login-subtitle">
          Acceso al asistente interno de Recursos Humanos y a la información autorizada para su
          cuenta.
        </p>
        <div className="login-access">
          <h2 className="visually-hidden">Iniciar sesion</h2>

          <a className="entra-button" href="/api/v1/auth/login" rel="noreferrer">
            <Icon name="microsoft" size={18} />
            Continuar con acceso corporativo
          </a>

          <div className="login-separator">
            <span>o acceso local</span>
          </div>

          {notice ? <p role="status">{notice}</p> : null}

          {error ? (
            <div className="banner" role="alert" data-testid="login-error">
              <Icon name="alert" size={17} />
              <p>{error}</p>
            </div>
          ) : null}

          <form className="login-form" onSubmit={handleSubmit} noValidate>
            <div className="field">
              <label htmlFor="username">Usuario</label>
              <input
                id="username"
                name="username"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                required
                maxLength={128}
                placeholder="Ingresa tu usuario"
              />
            </div>

            <div className="field">
              <div className="field-head">
                <label htmlFor="password">Contrasena</label>
                <button
                  className="field-toggle"
                  type="button"
                  aria-controls="password"
                  aria-pressed={showPassword}
                  onClick={() => setShowPassword((visible) => !visible)}
                >
                  {showPassword ? "Ocultar" : "Mostrar"}
                </button>
              </div>
              <input
                id="password"
                name="password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
                maxLength={256}
                placeholder="Ingresa tu contraseña"
              />
            </div>

            <button className="login-submit" type="submit" disabled={busy}>
              {busy ? "Verificando..." : "Entrar"}
            </button>
          </form>

          <p className="hint">
            Use el método de acceso habilitado por TI para su cuenta: usuario local o acceso
            corporativo.
          </p>
        </div>
        <div className="login-foot">
          <Icon name="shield" size={14} />
          <span>Matrix RH · interfaz {version}</span>
        </div>
      </section>
    </main>
  );
}
