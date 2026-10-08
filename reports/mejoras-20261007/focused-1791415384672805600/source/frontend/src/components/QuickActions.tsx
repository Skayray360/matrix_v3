/* Creado por Aldo Garcia. */
/** Accesos rapidos visuales. Nunca conceden permisos: solo envian una pregunta. */

import { useState } from "react";

import { Icon, type IconName } from "./Icon";

type QuickAction = {
  key: string;
  icon: IconName;
  title: string;
  description: string;
  prompt: string;
};

const ACTIONS: readonly QuickAction[] = [
  {
    key: "general",
    icon: "general",
    title: "General",
    description: "Guias para empleados",
    prompt: "Resume la documentacion general relevante para empleados.",
  },
  {
    key: "prestaciones",
    icon: "prestaciones",
    title: "Prestaciones",
    description: "Beneficios y vacaciones",
    prompt: "Explicame las prestaciones y politicas de vacaciones disponibles.",
  },
  {
    key: "nomina",
    icon: "nomina",
    title: "Nomina",
    description: "Procesos autorizados",
    prompt: "Explicame el proceso de nomina que puedo consultar.",
  },
  {
    key: "reclutamiento",
    icon: "reclutamiento",
    title: "Reclutamiento",
    description: "Seleccion y onboarding",
    prompt: "Explicame el proceso de reclutamiento y seleccion.",
  },
  {
    key: "relaciones",
    icon: "relaciones",
    title: "Relaciones",
    description: "Normativa laboral",
    prompt: "Resume los lineamientos de relaciones laborales que puedo consultar.",
  },
];

type Props = {
  disabled: boolean;
  onSelect: (prompt: string) => void;
};

/**
 * Los cinco temas ocupaban una franja fija que seguia ahi en el mensaje numero
 * veinte, empujando la conversacion hacia abajo. Ahora estan colapsados en una
 * fila de fichas y se despliegan con descripciones a peticion.
 */
export function QuickActions({ disabled, onSelect }: Props): JSX.Element {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="process-strip-wrap" aria-label="Temas de Recursos Humanos">
      {expanded ? (
        <>
          <div className="process-head">
            <span className="eyebrow">TEMAS DE RECURSOS HUMANOS</span>
            <button
              className="topic-toggle"
              type="button"
              disabled={disabled}
              aria-expanded={true}
              aria-controls="temas-rh"
              onClick={() => setExpanded(false)}
            >
              <span>Ocultar</span>
              <Icon name="chevronUp" size={14} />
            </button>
          </div>
          <ul className="process-strip" id="temas-rh">
            {ACTIONS.map((action) => (
              <li key={action.key}>
                <button
                  type="button"
                  className="process-card"
                  disabled={disabled}
                  onClick={() => onSelect(action.prompt)}
                  data-testid={`quick-action-${action.key}`}
                >
                  <span className="marker" aria-hidden="true">
                    <Icon name={action.icon} />
                  </span>
                  <span className="label">
                    <strong>{action.title}</strong>
                    <small>{action.description}</small>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <div className="topic-chips" id="temas-rh">
          <span className="topic-chips-label">Temas</span>
          {ACTIONS.map((action) => (
            <button
              key={action.key}
              type="button"
              className="topic-chip"
              disabled={disabled}
              onClick={() => onSelect(action.prompt)}
              title={action.description}
              data-testid={`quick-action-${action.key}`}
            >
              <Icon name={action.icon} size={15} />
              {action.title}
            </button>
          ))}
          <button
            className="topic-toggle"
            type="button"
            disabled={disabled}
            aria-expanded={false}
            aria-controls="temas-rh"
            onClick={() => setExpanded(true)}
          >
            <span>Ver descripciones</span>
            <Icon name="chevronDown" size={14} />
          </button>
        </div>
      )}
    </div>
  );
}
