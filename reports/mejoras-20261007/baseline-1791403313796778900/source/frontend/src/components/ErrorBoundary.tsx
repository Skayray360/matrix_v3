/* Creado por Aldo Garcia. */
/** Recuperacion visible ante un fallo de render, sin revelar contenido del error. */
import { Component, type ReactNode } from "react";

import { Mascot } from "./Mascot";

type Props = { children: ReactNode; onReload?: () => void };
type State = { failed: boolean };

function reloadApplication(): void {
  window.location.reload();
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(): void {
    // No enviar trazas, mensajes ni contenido de RH a servicios de terceros.
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;

    return (
      <main className="recovery-shell">
        <section className="recovery-card" role="alert" aria-labelledby="recovery-title">
          <Mascot className="recovery-mascot" />
          <h1 id="recovery-title">No se pudo mostrar Matrix RH</h1>
          <p>
            La interfaz encontró un error. Recargue la página para volver a abrir sus
            conversaciones. Puede ser necesario escribir de nuevo el mensaje que todavía no había
            enviado.
          </p>
          <button
            className="button"
            type="button"
            onClick={this.props.onReload ?? reloadApplication}
          >
            Recargar Matrix RH
          </button>
        </section>
      </main>
    );
  }
}
