<!-- Creado por Aldo Garcia. -->

# frontend/

Interfaz de Matrix RH: React, TypeScript y Vite. Conserva el diseño personalizado
IA Matrix del ZIP: fondo de acceso, mascota, conversación, adjuntos, citas,
trazabilidad, menú móvil y temas claro/oscuro. Sigue la organización de
`Skayray360/matrix_v2`: páginas, componentes, seguridad y servicios HTTP.

La interfaz y el backend publican la versión **1.3.1** y comparten el contrato
`/api/v1`.

| Ruta | Responsabilidad |
| --- | --- |
| `src/App.tsx`, `src/main.tsx` | Inicio y resolución de sesión del servidor. |
| `src/pages/` | Pantallas de acceso y conversación. |
| `src/components/` | Controles reutilizables, citas y trazabilidad. |
| `src/security/` | Renderizado de Markdown sin HTML arbitrario. |
| `src/services/` | Cliente HTTP tipado, cookie de sesión y CSRF. |
| `src/assets/`, `src/styles.css` | Imágenes originales y estilos de la interfaz. |
| `tests/` | Pruebas unitarias, visuales y recorridos contra la pila real. |
| `dist/` | Build incluido, servido por FastAPI en el mismo origen. |

El backend determina la identidad, autorización y categorías accesibles. La UI
refleja ese resultado; nunca almacena tokens de sesión en almacenamiento web.

Esta entrega utiliza únicamente `gemma4:latest` para generar respuestas y
`embeddinggemma:latest` para indexar y recuperar documentos, configurados en el
backend. No existe selector de modelos en el navegador.

## Instalación y compilación

Requiere Node.js **22.13.0 en la rama 22, o 24 y superior**. El gestor fijado es npm **11.9.0**,
y las versiones exactas están en `package-lock.json`. Desde esta carpeta:

```bash
corepack npm ci --ignore-scripts
corepack npm run lint
corepack npm run format:check
corepack npm run typecheck
corepack npm test
corepack npm run build
```

También puede usar `npm` directamente si su versión es la fijada. La opción
`--ignore-scripts` respeta `.npmrc`; el build comprueba que los binarios de Vite
quedaron disponibles. No modifique `dist/` a mano: vuelva a compilar `src/`.

En desarrollo, `corepack npm run dev` abre Vite en el puerto 5173 y redirige
`/api` a FastAPI en 127.0.0.1:8000. En la distribución final, FastAPI sirve
`dist/` y `/api/v1` desde el mismo origen; no se necesita el servidor Vite.

## Validación

La suite unitaria usa Vitest y Testing Library. Los nueve recorridos visuales
usan Chromium real y respuestas de API sintéticas:

```bash
corepack npm run e2e:install
corepack npm run test:visual
```

El recorrido completo `corepack npm run e2e` exige backend, base de datos,
modelos locales, corpus indexado y cuentas sintéticas en una instalación de
pruebas. Los recorridos visuales no verifican los modelos, MySQL ni SSO.
Consulte [Pruebas y aceptación](../docs/TESTING.md).

Un Error Boundary en la raíz muestra recuperación mediante recarga ante errores
de render y no revela detalles del fallo. Las respuestas de intención `general`
incluyen un aviso explícito de que no se basan en documentación de la empresa,
tanto al recibirlas como al volver a abrir el historial. Los errores HTTP siguen
el contrato tipado del backend.

Cada intercambio HTTP de estado, envío a cola o sesión tiene un límite de 30 s.
La generación continúa con el plazo del servidor; una interrupción de HTTP
conserva el borrador y su identificador para consultar la misma solicitud al
reintentar. Los adjuntos y el endpoint síncrono heredado permiten hasta 660 s.
El renderizador admite texto de tablas incompletas sin bloquear la página y
citas autorizadas de hasta 1024 caracteres. El historial muestra el nombre del
archivo y conserva el ID completo en la cita, sin inventar metadatos perdidos.

ESLint aplica reglas recomendadas de JavaScript/TypeScript y verifica el orden
y las dependencias de los hooks. Prettier comprueba el formato de `src/`,
`tests/` y configuraciones; `npm run format` permite normalizarlos. Ambos controles
se ejecutan en CI. Sus dependencias están fijadas con integridad en el lock y
se instalan con `--ignore-scripts`.

Los resultados, capturas y `node_modules/` son temporales y no se distribuyen.
Las credenciales se configuran en el backend, nunca dentro de esta carpeta.

## Componentes y contrato visual

| Componente | Responsabilidad |
| --- | --- |
| `LoginPage`, `ChatPage` | Acceso y conversación; autorización efectiva del backend |
| `Sidebar`, `MessageList` | Historial propio, turnos, fuentes y estados |
| `Composer` | Texto/adjuntos privados de esa conversación; teclado y drag & drop |
| `QuickActions` | Prompts ordinarios que vuelven a pasar por clasificación/autorización |
| `TracePanel` | Metadata de la última respuesta; se limpia al cambiar conversación |
| `Mascot`, `ThemeControl` | Marca local y cambio de tema |
| `ErrorBoundary` | Recuperación por recarga sin exponer detalles internos |

Mantener etiquetas accesibles, foco visible, navegación por teclado y `aria-live`
en mensajes/estados. Los assets `bot-mascot.png` y `login-bg.jpg` son locales, no
se descargan al abrir el chat. Conservar proporciones y texto alternativo apropiado.

`security/Markdown.tsx` construye elementos React, sin `dangerouslySetInnerHTML`.
HTML no confiable se muestra como texto. Soporta encabezados, párrafos, listas,
tablas, código, énfasis y citas Matrix. Una referencia no incluida en las fuentes
recibidas no se dibuja como cita verificada. Esto reduce riesgos del renderizador;
no certifica la veracidad de la respuesta ni elimina toda superficie de ataque.

## API usada por la interfaz

Todos los paths siguientes llevan el prefijo `/api/v1`. `api.ts` usa cookies
same-origin; no guarda tokens en `localStorage`/`sessionStorage`. El CSRF vive en
memoria y viaja en `X-CSRF-Token` para mutaciones autenticadas. Los errores tipados
incluyen `code`, `message`, `request_id`, nunca deben mostrar stack traces.

| Método y ruta | Uso |
| --- | --- |
| `GET /me` | Sesión, alcance y CSRF |
| `POST /auth/local/login`, `POST /auth/logout` | Acceso local de prueba y cierre de sesión |
| `GET /auth/login` | Inicio del flujo OIDC configurado |
| `GET`, `POST /conversations` | Listado y nueva conversación propia |
| `GET`, `DELETE /conversations/{id}` | Historial paginado o baja de conversación propia |
| `POST /chat` | Ruta síncrona compatible |
| `POST /chat/submit` | Admisión en cola; 202 no significa generación completa |
| `GET /chat/requests/{id}` | Estado, capacidad y resultado de solicitud propia |
| `POST /chat/requests/{id}/cancel` | Cancelación de publicación |
| `POST /conversations/{id}/attachments` | Carga privada multipart |
| `GET /documents/{id}/status` | Estado del documento autorizado |

La UI espera `completed`; `failed`, `expired` o `cancelled` no se presentan como
respuesta terminada. No añadir URLs de modelos, DSN ni decisiones de permisos
al cliente. El helper E2E correlaciona la solicitud y el mensaje nuevos; una
respuesta antigua no satisface la espera.

El perfil visual `playwright.visual.config.ts` inicia preview en 127.0.0.1:5174,
con Chromium y API sintética. `MATRIX_VISUAL_CHROMIUM` permite un navegador existente.
Capturas/trazas en `test-results/` no se empaquetan. `playwright.config.ts` conserva
el E2E contra la pila real de pruebas: no confundir ambos alcances.
