# Creado por Aldo Garcia.
# Docker es una alternativa Linux; instalar.bat no usa esta imagen.
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS frontend
WORKDIR /build/frontend
RUN apk add --no-cache python3 py3-yaml
COPY backend/scripts/verify_supply_chain.py /checks/backend/scripts/verify_supply_chain.py
COPY backend/app/common/errors.py /checks/backend/app/common/errors.py
COPY backend/config/supply_chain_denylist.yaml /checks/backend/config/supply_chain_denylist.yaml
ENV PYTHONPATH=/checks/backend COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN python3 /checks/backend/scripts/verify_supply_chain.py --frontend /build/frontend --lock-only
RUN corepack npm audit --audit-level=low --include=dev --include=optional --include=peer
RUN corepack npm ci --ignore-scripts --no-audit --no-fund --include=dev --include=optional --include=peer
COPY frontend/ ./
RUN python3 /checks/backend/scripts/verify_supply_chain.py --frontend /build/frontend
RUN corepack npm run build

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend PATH="/app/backend/.venv/bin:$PATH"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.12.23
RUN groupadd --gid 10001 matrixrh \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp matrixrh
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/Dockerfile ./backend/
COPY backend/app ./backend/app
COPY backend/scripts ./backend/scripts
COPY backend/seeds ./backend/seeds
COPY backend/migrations ./backend/migrations
RUN uv sync --project /app/backend --frozen --no-dev --no-editable
COPY backend/config ./backend/config
COPY backend/release ./backend/release
COPY instalar.bat iniciar.bat detener.bat diagnosticar.bat docker-compose.yml .htaccess ./
COPY frontend/package.json frontend/package-lock.json ./frontend/
COPY --from=frontend /build/frontend/dist ./frontend/dist
RUN mkdir -p /app/knowledge-base/documents /app/knowledge-base/state/uploads \
        /app/knowledge-base/state/run /app/backend/logs /app/backend/config/secrets \
    && chown -R matrixrh:matrixrh /app/knowledge-base /app/backend/logs
USER matrixrh
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=8s --start-period=45s --retries=12 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)" || exit 1
WORKDIR /app/backend
ENTRYPOINT ["python", "-m", "scripts.docker_entrypoint"]
CMD ["serve"]
