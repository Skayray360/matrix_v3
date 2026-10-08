#!/usr/bin/env bash
# Creado por Aldo Garcia.
# ---------------------------------------------------------------------------
# Equivalente shell de los .bat de la raiz para entornos no-Windows.
# No duplica logica: delega en los mismos modulos Python del backend.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PY="${ROOT}/.venv/bin/python"
BACKEND="${ROOT}/backend"
PID_FILE="${ROOT}/var/matrixrh-backend.pid"
LOG_DIR="${ROOT}/var/logs"

info()  { printf '  \033[0;90m-> %s\033[0m\n' "$1"; }
ok()    { printf '  \033[0;32m[ OK ]\033[0m %s\n' "$1"; }
warn()  { printf '  \033[0;33m[WARN]\033[0m %s\n' "$1"; }
fail()  { printf '  \033[0;31m[FAIL]\033[0m %s\n' "$1"; }
section() { printf '\n\033[0;36m=== %s ===\033[0m\n' "$1"; }

require_python312() {
    if ! command -v python3.12 >/dev/null 2>&1; then
        fail "Se requiere Python 3.12. Instalelo y vuelva a ejecutar."
        exit 1
    fi
    python3.12 -c 'import struct,sys; sys.exit(0 if sys.version_info[:2] == (3,12) and struct.calcsize("P") == 8 else 1)' || {
        fail "Se requiere exactamente Python 3.12 de 64 bits."; exit 1;
    }
    ok "Python 3.12 x64 disponible"
}

py() {
    # Ejecuta un modulo del backend con el PYTHONPATH correcto.
    ( cd "${BACKEND}" && PYTHONPATH="${BACKEND}" "${VENV_PY}" "$@" )
}

backend_url() {
    py -c 'from app.config import get_settings; s=get_settings(); host={"0.0.0.0":"127.0.0.1","::":"::1"}.get(s.app_host,s.app_host); host=f"[{host}]" if ":" in host else host; print(f"http://{host}:{s.app_port}")'
}

process_owned() {
    # No basta con un PID: puede haberse reutilizado despues de un reinicio.
    "${VENV_PY}" - "${ROOT}" "$1" <<'PY_CHECK'
import os
import sys
from pathlib import Path
import psutil
try:
    root = Path(sys.argv[1]).resolve()
    process = psutil.Process(int(sys.argv[2]))
    args = process.cmdline()
    expected = root / '.venv/bin/python'
    owned = (len(args) >= 4 and Path(args[0]).absolute() == expected
             and args[1:4] == ['-m', 'scripts.bootstrap', 'serve']
             and Path(process.cwd()).resolve() == root / 'backend'
             and Path(process.exe()).resolve() == expected.resolve())
except (OSError, ValueError, psutil.Error):
    owned = False
sys.exit(0 if owned else 1)
PY_CHECK
}

backend_state() {
    "${VENV_PY}" - "$1" "$2" <<'PY_CHECK'
import json
import sys
import urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + sys.argv[2], timeout=3) as response:
        data = json.load(response)
    valid = data.get('app') == 'Matrix RH' if sys.argv[2] == '/health' else data.get('ready') is True
except Exception:
    valid = False
sys.exit(0 if valid else 1)
PY_CHECK
}

cmd_install() {
    section "MATRIX RH - INSTALACION"
    require_python312
    local rebuild=0 offline=0 uv_bin
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --rebuild-frontend) rebuild=1 ;;
            --offline) offline=1 ;;
            *) fail "Opcion desconocida: $1"; exit 1 ;;
        esac
        shift
    done
    ( cd "${BACKEND}" && python3.12 -S -m scripts.preflight --integrity-only --require-manifest )

    # uv es obligatorio, igual que en el instalador de Windows: garantiza una
    # instalacion REPRODUCIBLE desde uv.lock (no resuelve rangos ni trae una
    # version publicada hace minutos).
    uv_bin="$(command -v uv || true)"
    if [ -z "${uv_bin}" ] && [ -x "${ROOT}/var/tools/uv/bin/uv" ]; then
        uv_bin="${ROOT}/var/tools/uv/bin/uv"
    fi
    if [ -z "${uv_bin}" ]; then
        if [ "${offline}" -eq 1 ]; then
            fail "Falta uv. Prepare uv y la cache antes de usar --offline."; exit 1;
        fi
        python3.12 -m venv "${ROOT}/var/tools/uv"
        "${ROOT}/var/tools/uv/bin/python" -m pip install --disable-pip-version-check --no-input --only-binary=:all: --no-deps 'uv==0.12.19'
        uv_bin="${ROOT}/var/tools/uv/bin/uv"
    fi
    ( cd "${BACKEND}" && python3.12 -m scripts.uv_runtime --banner "$("${uv_bin}" --version)" )

    if [ ! -f "${ROOT}/.env" ]; then
        ( umask 077; cp "${ROOT}/.env.example" "${ROOT}/.env" )
        chmod 600 "${ROOT}/.env"
        # La clave de firma se genera localmente; nunca viaja en el paquete.
        local secret
        secret="$(python3.12 -c 'import secrets; print(secrets.token_hex(32))')"
        sed -i.bak "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=${secret}|" "${ROOT}/.env"
        rm -f "${ROOT}/.env.bak"
        ok "Se creo .env con una APP_SECRET_KEY nueva"
    else
        ok ".env ya existe (no se sobrescribe)"
    fi

    mkdir -p "${LOG_DIR}" "${ROOT}/var/uploads" "${ROOT}/var/qdrant" \
        "${ROOT}/reports/tests" "${ROOT}/reports/security"

    info "Sincronizando el backend desde uv.lock (--frozen)..."
    local -a sync_args=(sync --frozen --no-dev --python python3.12 --no-python-downloads)
    if [ "${offline}" -eq 1 ]; then sync_args+=(--offline); fi
    ( cd "${BACKEND}" && UV_PROJECT_ENVIRONMENT="${ROOT}/.venv" "${uv_bin}" "${sync_args[@]}" )
    py -m scripts.preflight --dependencies-only
    # Clave aleatoria local; nunca se imprime ni se cambian cuentas existentes.
    py - "${ROOT}/.env" <<'PY_SEED'
import os
import secrets
import sys
from pathlib import Path
from dotenv import dotenv_values, set_key
path = Path(sys.argv[1])
values = dict(dotenv_values(path))
values.update(os.environ)
profile = values.get('APP_ENV', 'development').lower()
seed = values.get('LOCAL_TEST_SEED_USERS_ENABLED', 'true').lower()
if profile in {'development', 'test'} and seed in {'1', 'true', 'yes', 'on', 't', 'y'}:
    if not values.get('MATRIX_SEED_PASSWORD'):
        if 'MATRIX_SEED_PASSWORD' in os.environ:
            raise SystemExit('MATRIX_SEED_PASSWORD heredada esta vacia; retire esa variable y repita.')
        set_key(str(path), 'MATRIX_SEED_PASSWORD', secrets.token_hex(32), quote_mode='never')
        print('Clave de acceso inicial creada en MATRIX_SEED_PASSWORD de .env.')
PY_SEED
    py -m scripts.bootstrap upgrade-config
    py -m scripts.bootstrap pin-models
    py -m scripts.preflight --llm-only
    py -m scripts.model_smoke_test --run-inference --profile all
    ok "Dependencias del backend sincronizadas exactamente desde uv.lock"

    if [ "${rebuild}" -eq 0 ]; then
        [ -f "${ROOT}/frontend/dist/index.html" ] || { fail "Falta frontend/dist. Extraiga el ZIP completo o use --rebuild-frontend."; exit 1; }
        ok "Se usa la interfaz compilada incluida; no requiere Node/Corepack"
    else
        if ! command -v node >/dev/null 2>&1 || ! command -v corepack >/dev/null 2>&1; then
            fail "Se requieren Node 22.13+ y Corepack para compilar la interfaz."
            exit 1
        fi
        node -e 'const [major,minor]=process.versions.node.split(".").map(Number); if((major<24 && major!==22) || (major===22 && minor<13)) process.exit(1)' || {
            fail "La version de Node no cumple ^22.13.0 o >=24."; exit 1;
        }
        export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
        info "Instalando el frontend mediante el gestor con hash y package-lock.json..."
        py -m scripts.verify_supply_chain --lock-only || { fail "Lockfile bloqueado; no se descargan paquetes."; exit 1; }
        if [ "${offline}" -eq 1 ]; then
            export COREPACK_ENABLE_NETWORK=0
            warn "Offline: no se consultaron avisos npm actuales; requiere cache validada."
        else
            ( cd "${ROOT}/frontend" && corepack npm audit --audit-level=low --include=dev --include=optional --include=peer ) || {
                fail "npm audit encontro avisos o no pudo verificarlos; no se descargan paquetes."; exit 1;
            }
        fi
        local -a npm_extra=()
        if [ "${offline}" -eq 1 ]; then npm_extra+=(--offline); fi
        ( cd "${ROOT}/frontend" && corepack npm ci --ignore-scripts --no-audit --no-fund --include=dev --include=optional --include=peer "${npm_extra[@]}" )
        py -m scripts.verify_supply_chain || { fail "Fallo la cadena de suministro; no se compila."; exit 1; }
        ( cd "${ROOT}/frontend" && corepack npm run build )
        ok "Frontend compilado sin ejecutar scripts de instalacion"

    fi


    py -m scripts.preflight --app-only
    py -m scripts.preflight --safety-only --read-only

    py -m scripts.bootstrap setup
    py -m scripts.preflight
}

cmd_start() {
    section "MATRIX RH - ARRANQUE"
    local url; url="$(backend_url)"

    if backend_state "${url}" /health; then
        if [ -f "${PID_FILE}" ] && process_owned "$(cat "${PID_FILE}")" && backend_state "${url}" /ready \
            && [ -f "${ROOT}/frontend/dist/index.html" ]; then
            ok "Matrix RH ya esta listo en ${url}"
            return 0
        fi
        fail "El puerto responde pero no es un proceso propio listo, o falta la interfaz."
        exit 1
    fi

    [ -f "${ROOT}/frontend/dist/index.html" ] || { fail "Falta frontend/dist; ejecute install."; exit 1; }
    py -m scripts.preflight || { fail "El preflight fallo: no se arranca"; exit 1; }

    mkdir -p "${LOG_DIR}"
    local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
    (
        cd "${BACKEND}"
        PYTHONPATH="${BACKEND}" nohup "${VENV_PY}" -m scripts.bootstrap serve --skip-preflight \
            >"${LOG_DIR}/backend-${stamp}.log" 2>"${LOG_DIR}/backend-${stamp}.err.log" &
        echo $! > "${PID_FILE}"
    )
    local backend_pid; backend_pid="$(cat "${PID_FILE}")"
    info "PID ${backend_pid}  logs en ${LOG_DIR}"

    local waited=0
    while [ "${waited}" -lt 180 ]; do
        if ! kill -0 "${backend_pid}" 2>/dev/null; then break; fi
        if backend_state "${url}" /health && backend_state "${url}" /ready; then
            ok "Backend e interfaz listos en ${url}"
            return 0
        fi
        sleep 2; waited=$((waited + 2))
    done
    fail "El backend no alcanzo /health y /ready en 180 s"
    if process_owned "${backend_pid}"; then kill "${backend_pid}"; fi
    rm -f "${PID_FILE}"
    tail -n 20 "${LOG_DIR}/backend-${stamp}.err.log" || true
    exit 1
}

cmd_stop() {
    section "MATRIX RH - DETENER"
    if [ -f "${PID_FILE}" ]; then
        local backend_pid; backend_pid="$(cat "${PID_FILE}")"
        if process_owned "${backend_pid}"; then
            kill "${backend_pid}"; ok "Detenido PID ${backend_pid}"
        else
            warn "PID ausente o ajeno: no se detiene ningun proceso."
        fi
        rm -f "${PID_FILE}"
    else
        ok "Matrix RH no estaba en ejecucion"
    fi
}

cmd_diagnose() {
    section "MATRIX RH - DIAGNOSTICO"
    py -m scripts.preflight --read-only
}

cmd_validate() {
    section "MATRIX RH - VALIDACION"
    # Este gate precede incluso al stop: validar nunca modifica la instalacion
    # operativa si no se autorizo una copia descartable completa de pruebas.
    py -m scripts.test_environment
    cmd_stop
    py -m scripts.bootstrap migrate
    py -m scripts.bootstrap seed
    py -m scripts.bootstrap ingest
    py -m scripts.run_quality_gate --fast
    # --fast conserva el alcance de recuperacion sin generacion de este script.
    # El gate devuelve INCOMPLETE (2), nunca aprobado, si omite el E2E completo.
}

usage() {
    cat <<'EOF'
Uso: ./scripts/matrixrh.sh <comando>

  install    Instalacion idempotente; usa frontend incluido (--rebuild-frontend/--offline opcionales)
  start      Arranca el backend (preflight + espera a /health y /ready)
  stop       Detiene el backend
  diagnose   Diagnostico de solo lectura
  validate   Migraciones, seed, ingesta, pruebas, RAG y secretos

En Windows use los .bat de la raiz.
EOF
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
case "${1:-}" in
    install)  shift; cmd_install "$@"; cmd_start ;;
    start)    cmd_start ;;
    stop)     cmd_stop ;;
    diagnose) cmd_diagnose ;;
    validate) cmd_validate ;;
    *)        usage; exit 1 ;;
esac
fi
