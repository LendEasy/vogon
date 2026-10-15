#!/usr/bin/env bash
# Voice Gateway — start the standalone gateway process (M3 relocated home).
#   ./run.sh                        # 0.0.0.0:8002 (dev; prod unit uses 8010)
set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(cd .. && pwd)"
HOST="${GATEWAY_HOST:-0.0.0.0}"
PORT="${GATEWAY_PORT:-8002}"
# The gateway's own venv (install.sh) first; the monorepo's shared one as the fallback.
PYBIN="$(pwd)/.venv/bin/python3"
[ -x "$PYBIN" ] || PYBIN="${REPO_ROOT}/data-plane/.venv/bin/python3"
[ -x "$PYBIN" ] || PYBIN="python3"
echo "voice gateway starting on ${HOST}:${PORT} (capability policy: ${GATEWAY_CAPABILITY_POLICY:-refuse})"
exec env PYTHONPATH="${REPO_ROOT}/data-plane:${REPO_ROOT}/voice-gateway:${REPO_ROOT}/vogon-contract/src" \
    "$PYBIN" -m uvicorn vogon.gateway_service.main:app \
    --host "$HOST" --port "$PORT" --log-level info
