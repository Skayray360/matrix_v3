/* Creado por Aldo Garcia. */
/**
 * Juego de iconos de Matrix RH.
 *
 * Antes la interfaz usaba caracteres tipograficos como iconos (`◫`, `✕`, `＋`,
 * `↗`, `●`). Cada sistema operativo los dibuja con una fuente distinta, no
 * heredan el grosor del trazo y no se alinean con el texto que acompanan.
 *
 * Aqui son SVG de trazo sobre una rejilla de 20 px, con `currentColor`, de modo
 * que un icono toma el color y el tamano del control que lo contiene. Siempre
 * son decorativos: el nombre accesible lo pone el boton, con texto visible o
 * con `aria-label`.
 */

export type IconName =
  | "menu"
  | "close"
  | "plus"
  | "search"
  | "trash"
  | "paperclip"
  | "send"
  | "shield"
  | "panel"
  | "check"
  | "alert"
  | "chevronDown"
  | "chevronUp"
  | "sun"
  | "moon"
  | "monitor"
  | "chat"
  | "spinner"
  | "microsoft"
  | "general"
  | "prestaciones"
  | "nomina"
  | "reclutamiento"
  | "relaciones";

type Props = {
  name: IconName;
  /** Lado del cuadro en px. 16 dentro de texto, 18 en botones, 20 o mas suelto. */
  size?: number;
  className?: string;
};

const PATHS: Record<IconName, JSX.Element> = {
  menu: (
    <path d="M3.4 5.6h13.2M3.4 10h13.2M3.4 14.4h13.2" strokeWidth="1.7" strokeLinecap="round" />
  ),
  close: <path d="M5.5 5.5 14.5 14.5M14.5 5.5 5.5 14.5" strokeWidth="1.8" strokeLinecap="round" />,
  plus: <path d="M10 4.2v11.6M4.2 10h11.6" strokeWidth="2" strokeLinecap="round" />,
  search: (
    <>
      <circle cx="9" cy="9" r="5.4" strokeWidth="1.6" />
      <path d="m13.2 13.2 3.1 3.1" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
  trash: (
    <path
      d="M3.8 5.8h12.4M8 5.6V4.2h4v1.4M5.6 5.8l.7 10a1.4 1.4 0 0 0 1.4 1.3h4.6a1.4 1.4 0 0 0 1.4-1.3l.7-10"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
  paperclip: (
    <path
      d="M14.6 9.3 9.9 14a3.2 3.2 0 0 1-4.5-4.5l5.4-5.4a2.1 2.1 0 1 1 3 3l-5.4 5.4a1 1 0 0 1-1.5-1.5l4.7-4.7"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
  send: (
    <path
      d="M10 15.8V4.6M5.2 9.4 10 4.4l4.8 5"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
  shield: (
    <>
      <path
        d="M10 2.5 3.75 5v4.6c0 3.5 2.5 6.6 6.25 7.9 3.75-1.3 6.25-4.4 6.25-7.9V5L10 2.5Z"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="m7.4 10 1.9 1.9 3.4-3.6"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </>
  ),
  panel: (
    <>
      <rect x="2.6" y="3.6" width="14.8" height="12.8" rx="2.2" strokeWidth="1.5" />
      <path d="M12.6 3.6v12.8" strokeWidth="1.5" />
    </>
  ),
  check: (
    <path
      d="m4.5 10.4 3.6 3.6 7.4-8"
      strokeWidth="2.3"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
  alert: (
    <>
      <circle cx="10" cy="10" r="7.6" strokeWidth="1.5" />
      <path d="M10 6.2v4.6M10 13.4v.1" strokeWidth="1.8" strokeLinecap="round" />
    </>
  ),
  chevronDown: (
    <path
      d="m5.5 8 4.5 4.4L14.5 8"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
  chevronUp: (
    <path
      d="m5.5 12 4.5-4.4 4.5 4.4"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  ),
  sun: (
    <>
      <circle cx="10" cy="10" r="3.4" strokeWidth="1.6" />
      <path
        d="M10 2.6v1.8M10 15.6v1.8M17.4 10h-1.8M4.4 10H2.6M15.2 4.8l-1.3 1.3M6.1 13.9l-1.3 1.3M15.2 15.2l-1.3-1.3M6.1 6.1 4.8 4.8"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </>
  ),
  moon: (
    <path
      d="M16 11.6A6.6 6.6 0 0 1 8.4 4a6.8 6.8 0 1 0 7.6 7.6Z"
      strokeWidth="1.6"
      strokeLinejoin="round"
    />
  ),
  monitor: (
    <>
      <rect x="2.6" y="4" width="14.8" height="9.6" rx="1.8" strokeWidth="1.6" />
      <path d="M7.4 16.6h5.2M10 13.6v3" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
  chat: (
    <path
      d="M4.2 4.6h11.6a1.4 1.4 0 0 1 1.4 1.4v6.4a1.4 1.4 0 0 1-1.4 1.4H8.4L5 16.4v-2.6H4.2a1.4 1.4 0 0 1-1.4-1.4V6a1.4 1.4 0 0 1 1.4-1.4Z"
      strokeWidth="1.5"
      strokeLinejoin="round"
    />
  ),
  spinner: (
    <>
      <circle cx="10" cy="10" r="7.2" strokeWidth="1.6" opacity=".3" />
      <path d="M17.2 10A7.2 7.2 0 0 0 10 2.8" strokeWidth="1.8" strokeLinecap="round" />
    </>
  ),
  microsoft: (
    <>
      <rect x="2.6" y="2.6" width="6.6" height="6.6" fill="currentColor" stroke="none" />
      <rect
        x="10.8"
        y="2.6"
        width="6.6"
        height="6.6"
        fill="currentColor"
        stroke="none"
        opacity=".62"
      />
      <rect
        x="2.6"
        y="10.8"
        width="6.6"
        height="6.6"
        fill="currentColor"
        stroke="none"
        opacity=".62"
      />
      <rect x="10.8" y="10.8" width="6.6" height="6.6" fill="currentColor" stroke="none" />
    </>
  ),
  general: (
    <path
      d="M3.6 4.6h4.2c1.2 0 2.2.9 2.2 2v8.8c0-.9-.9-1.6-2-1.6H3.6V4.6Zm12.8 0h-4.2c-1.2 0-2.2.9-2.2 2v8.8c0-.9.9-1.6 2-1.6h4.4V4.6Z"
      strokeWidth="1.5"
      strokeLinejoin="round"
    />
  ),
  prestaciones: (
    <path
      d="M10 16.4S3.9 12.9 3.9 8.6a3.1 3.1 0 0 1 6.1-.9 3.1 3.1 0 0 1 6.1.9c0 4.3-6.1 7.8-6.1 7.8Z"
      strokeWidth="1.5"
      strokeLinejoin="round"
    />
  ),
  nomina: (
    <>
      <rect x="2.6" y="5.2" width="14.8" height="9.6" rx="1.8" strokeWidth="1.5" />
      <circle cx="10" cy="10" r="2.2" strokeWidth="1.5" />
    </>
  ),
  reclutamiento: (
    <>
      <circle cx="8.2" cy="7.4" r="2.9" strokeWidth="1.5" />
      <path
        d="M2.9 16.1c.4-2.6 2.6-4.2 5.3-4.2 1.1 0 2.1.3 2.9.7"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
      <path d="M14.6 11.3v4.6M12.3 13.6h4.6" strokeWidth="1.5" strokeLinecap="round" />
    </>
  ),
  relaciones: (
    <>
      <path d="M10 3.4v13.2M5.4 5.6h9.2" strokeWidth="1.5" strokeLinecap="round" />
      <path
        d="M5.4 5.8 3 11.2h4.8L5.4 5.8Zm9.2 0L12.2 11.2H17L14.6 5.8Z"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      <path d="M7.2 16.6h5.6" strokeWidth="1.5" strokeLinecap="round" />
    </>
  ),
};

export function Icon({ name, size = 18, className }: Props): JSX.Element {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}

/** Monograma de Matrix RH. Antes era la letra `M` suelta dentro de un cuadro. */
export function BrandMark({
  size = 20,
  className,
}: {
  size?: number;
  className?: string;
}): JSX.Element {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d="M3.5 15V5l6.5 6.2L16.5 5v10"
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
