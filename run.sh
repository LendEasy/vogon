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
# The echo canceller's alignment check is NumPy code. With the BLAS default of one worker thread per
# core the workers spin between 20 ms audio frames: measured 2026-11-17, about 96% of a core per call
# against 2.6% with one thread. Set before the process starts; an exported value still wins.
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}" OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
echo "voice gateway starting on ${HOST}:${PORT} (capability policy: ${GATEWAY_CAPABILITY_POLICY:-refuse})"
exec env PYTHONPATH="${REPO_ROOT}/data-plane:${REPO_ROOT}/voice-gateway:${REPO_ROOT}/vogon-contract/src" \
    "$PYBIN" -m uvicorn vogon.gateway_service.main:app \
    --host "$HOST" --port "$PORT" --log-level info
