/* Creado por Aldo Garcia. */
/** Imagen local tomada de la referencia visual; no carga recursos externos. */
import mascot from "../assets/bot-mascot.png";

type Props = { className?: string; alt?: string };

export function Mascot({ className, alt = "" }: Props): JSX.Element {
  return (
    <img
      className={className}
      src={mascot}
      alt={alt}
      aria-hidden={alt === "" ? true : undefined}
      width={341}
      height={512}
      decoding="async"
    />
  );
}
