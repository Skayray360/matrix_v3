/* Creado por Aldo Garcia. */
/** Cambio de contraseña de la cuenta local actual; el servidor valida y renueva la sesión. */

import { useEffect, useRef, useState, type FormEvent } from "react";

import { api, ApiError, type Me } from "../services/api";
import { Icon } from "./Icon";

type Props = {
  onChanged: (profile: Me) => void;
  onClose: () => void;
  onSessionExpired?: () => void;
};

export function ChangePasswordDialog({ onChanged, onClose, onSessionExpired }: Props): JSX.Element {
  const dialog = useRef<HTMLDialogElement>(null);
  const currentField = useRef<HTMLInputElement>(null);
  const doneButton = useRef<HTMLButtonElement>(null);
  const submitting = useRef(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [changed, setChanged] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement;
    element?.showModal();
    currentField.current?.focus();
    return () => {
      element?.close();
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, []);

  useEffect(() => {
    if (changed) doneButton.current?.focus();
  }, [changed]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (submitting.current) return;
    setError(null);

    const length = [...newPassword].length;
    if (!currentPassword) {
      setError("Escriba su contraseña actual.");
      currentField.current?.focus();
      return;
    }
    if (length < 16 || length > 128) {
      setError("La nueva contraseña debe tener entre 16 y 128 caracteres.");
      return;
    }
    if (newPassword !== newPassword.trim()) {
      setError("La nueva contraseña no debe tener espacios al inicio o al final.");
      return;
    }
    if (newPassword === currentPassword) {
      setError("Elija una contraseña diferente de la actual.");
      return;
    }
    if (newPassword !== confirmation) {
      setError("La confirmación no coincide con la nueva contraseña.");
      return;
    }

    submitting.current = true;
    setBusy(true);
    try {
      const profile = await api.changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmation("");
      onChanged(profile);
      setChanged(true);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401 && onSessionExpired) {
        setCurrentPassword("");
        setNewPassword("");
        setConfirmation("");
        onSessionExpired();
        return;
      }
      setError(
        caught instanceof ApiError
          ? `${caught.message}${caught.requestId ? ` (referencia: ${caught.requestId})` : ""}`
          : "No fue posible confirmar el cambio. Compruebe la conexión e inténtelo nuevamente.",
      );
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialog}
      className="password-dialog"
      aria-labelledby="password-dialog-title"
      aria-describedby="password-dialog-description"
      onCancel={(event) => {
        event.preventDefault();
        if (!submitting.current) onClose();
      }}
    >
      <div className="password-dialog-head">
        <h2 id="password-dialog-title">Cambiar contraseña</h2>
        <button
          className="icon-button"
          type="button"
          aria-label="Cerrar cambio de contraseña"
          disabled={busy}
          onClick={onClose}
        >
          <Icon name="close" />
        </button>
      </div>
      {changed ? (
        <>
          <p id="password-dialog-description" role="status">
            Contraseña actualizada. Las demás sesiones se cerraron y esta sesión se renovó.
          </p>
          <div className="password-dialog-actions">
            <button ref={doneButton} className="button" type="button" onClick={onClose}>
              Listo
            </button>
          </div>
        </>
      ) : (
        <>
          <p id="password-dialog-description">
            Use entre 16 y 128 caracteres. Al guardar se cerrarán las demás sesiones de su cuenta.
          </p>
          {error ? (
            <div className="banner" role="alert">
              <Icon name="alert" size={17} />
              <p>{error}</p>
            </div>
          ) : null}
          <form onSubmit={handleSubmit} noValidate aria-busy={busy}>
            <div className="field">
              <label htmlFor="current-password">Contraseña actual</label>
              <input
                ref={currentField}
                id="current-password"
                name="current_password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                required
                maxLength={256}
                disabled={busy}
              />
            </div>
            <div className="field">
              <label htmlFor="new-password">Nueva contraseña</label>
              <input
                id="new-password"
                name="new_password"
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                aria-describedby="password-dialog-description"
                required
                maxLength={256}
                disabled={busy}
              />
            </div>
            <div className="field">
              <label htmlFor="confirm-password">Confirmar nueva contraseña</label>
              <input
                id="confirm-password"
                name="confirm_password"
                type="password"
                autoComplete="new-password"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                required
                maxLength={256}
                disabled={busy}
              />
            </div>
            <div className="password-dialog-actions">
              <button className="secondary-button" type="button" disabled={busy} onClick={onClose}>
                Cancelar
              </button>
              <button className="button" type="submit" disabled={busy}>
                {busy ? "Guardando…" : "Guardar contraseña"}
              </button>
            </div>
          </form>
        </>
      )}
    </dialog>
  );
}
