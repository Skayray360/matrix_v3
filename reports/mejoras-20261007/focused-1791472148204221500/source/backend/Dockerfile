# Creado por Aldo Garcia.
# ---------------------------------------------------------------------------
# Imagen del backend de Matrix RH.
# Multi-stage: el frontend se compila en una etapa con Node y sólo el build
# resultante viaja a la imagen final, que no lleva Node ni herramientas de
# compilación.
# El proceso corre como usuario NO root.
# ---------------------------------------------------------------------------

# ------------------------------------------------------------------ frontend --
FROM node:22-alpine AS frontend
WORKDIR /build/frontend
# El mismo gate que Windows/Linux debe inspeccionar antes de ejecutar Vite.
# Python/PyYAML son herramientas de esta etapa; no viajan por este motivo a runtime.
RUN apk add --no-cache python3 py3-yaml
COPY backend/scripts/verify_supply_chain.py /checks/backend/scripts/verify_supply_chain.py
COPY backend/app/common/errors.py /checks/backend/app/common/errors.py
COPY config/supply_chain_denylist.yaml /checks/config/supply_chain_denylist.yaml
ENV PYTHONPATH=/checks/backend
# npm se provisiona por corepack (viene con Node) y su version se fija en el
# campo "packageManager" de package.json: la imagen usa exactamente esa version.
# El flag desactiva el prompt de descarga de corepack (colgaria el build).
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN python3 /checks/backend/scripts/verify_supply_chain.py --frontend /build/frontend --lock-only
RUN corepack npm audit --audit-level=low --include=dev --include=optional --include=peer
# `npm ci --ignore-scripts`, nunca una instalacion sin lockfile:
#   * `ci` instala exactamente lo que fija package-lock.json y FALLA
#     si el lockfile no esta sincronizado (no resuelve rangos ni trae una version
#     publicada hace minutos). Una instalacion no reproducible no llega a la imagen.
#   * `--ignore-scripts` impide preinstall/install/postinstall, el vector de
#     propagacion de los gusanos del ecosistema npm.
RUN corepack npm ci --ignore-scripts --no-audit --no-fund --include=dev --include=optional --include=peer
COPY frontend/ ./
RUN python3 /checks/backend/scripts/verify_supply_chain.py --frontend /build/frontend
RUN corepack npm run build

# ------------------------------------------------------------------- backend --
FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend \
    PATH="/app/backend/.venv/bin:$PATH"

# Dependencias del sistema mínimas: argon2-cffi y cryptography traen ruedas
# precompiladas, así que no hace falta toolchain de compilación.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Usuario sin privilegios.
RUN groupadd --system matrixrh && useradd --system --gid matrixrh --home /app matrixrh

WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock ./backend/
COPY backend/app ./backend/app
COPY backend/scripts ./backend/scripts
COPY backend/seeds ./backend/seeds
COPY backend/migrations ./backend/migrations
RUN uv sync --project /app/backend --frozen --no-dev --no-editable

COPY config ./config
COPY --from=frontend /build/frontend/dist ./frontend/dist

# Directorios de estado runtime, propiedad del usuario sin privilegios.
RUN mkdir -p /app/var/uploads /app/var/qdrant /app/var/logs /app/data/knowledge \
    && chown -R matrixrh:matrixrh /app

USER matrixrh

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=10s --start-period=40s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

WORKDIR /app/backend

# El arranque aplica migraciones idempotentes y luego sirve. El preflight se
# omite aquí porque el orquestador ya espera al healthcheck de las dependencias.
CMD ["sh", "-c", "python -m scripts.bootstrap migrate && python -m scripts.bootstrap serve --skip-preflight --host 0.0.0.0 --port 8000"]
