/* Creado por Aldo Garcia. */
/**
 * Selector de tema.
 *
 * `styles.css` ya definia una paleta oscura completa, pero solo se activaba
 * cambiando la preferencia del sistema operativo: dentro de la aplicacion no
 * habia forma de verla. Este control expone las tres opciones reales — claro,
 * oscuro y seguir al sistema — escribiendo `data-theme` en `<html>`.
 *
 * La preferencia vive en `localStorage`. Es una opcion de presentacion, no un
 * dato de sesion: aqui no se guarda ningun token ni identificador de usuario.
 */

import { useCallback, useEffect, useState } from "react";

import { Icon, type IconName } from "./Icon";

export type ThemeMode = "light" | "dark" | "system";

const STORAGE_KEY = "matrixrh.theme";
const ORDER: readonly ThemeMode[] = ["system", "light", "dark"];

const LABEL: Record<ThemeMode, string> = {
  system: "Tema: seguir al sistema",
  light: "Tema: claro",
  dark: "Tema: oscuro",
};

const GLYPH: Record<ThemeMode, IconName> = {
  system: "monitor",
  light: "sun",
  dark: "moon",
};

function readStored(): ThemeMode {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    if (value === "light" || value === "dark" || value === "system") return value;
  } catch {
    // Almacenamiento bloqueado (modo privado, politica del navegador): se
    // sigue al sistema, que es el comportamiento previo.
  }
  return "system";
}

function applyTheme(mode: ThemeMode): void {
  const root = document.documentElement;
  if (mode === "system") {
    delete root.dataset.theme;
  } else {
    root.dataset.theme = mode;
  }
}

export function useTheme(): [ThemeMode, () => void] {
  const [mode, setMode] = useState<ThemeMode>(readStored);

  useEffect(() => {
    applyTheme(mode);
    try {
      window.localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      // Sin persistencia la eleccion dura lo que dure la pestana.
    }
  }, [mode]);

  const cycle = useCallback(() => {
    setMode((current) => ORDER[(ORDER.indexOf(current) + 1) % ORDER.length]);
  }, []);

  return [mode, cycle];
}

export function ThemeControl(): JSX.Element {
  const [mode, cycle] = useTheme();

  return (
    <button
      className="icon-button"
      type="button"
      onClick={cycle}
      aria-label={LABEL[mode]}
      title={LABEL[mode]}
      data-testid="theme-toggle"
    >
      <Icon name={GLYPH[mode]} />
    </button>
  );
}
