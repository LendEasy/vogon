#!/usr/bin/env bash
# vogon — install into a local virtualenv and verify the native audio
# capabilities. Runs unprivileged; prints the one admin command when the echo
# canceller's build libraries are missing instead of attempting it.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
CONTRACT="../vogon-contract"

echo "── vogon install ─────────────────────────────────────────────────"
echo "[1/4] native build libraries (SpeexDSP echo canceller)"
_missing=""
if command -v dpkg >/dev/null 2>&1; then
    for _pkg in libspeex-dev libspeexdsp-dev swig g++ git; do
        dpkg -s "$_pkg" >/dev/null 2>&1 || _missing="$_missing $_pkg"
    done
fi
if [ -n "$_missing" ]; then
    echo "    MISSING:$_missing — ask an admin to run once:"
    echo "        sudo apt-get install -y git g++ swig libspeex-dev libspeexdsp-dev"
    echo "    Continuing; step 4 reports whether AEC built."
else
    echo "    all present"
fi

echo "[2/4] python venv + packages"
$PY - <<'PYEOF'
import sys
if sys.version_info >= (3, 13):
    print("    note: Python >= 3.13 — audioop comes from the audioop-lts wheel")
PYEOF
[ -d .venv ] || $PY -m venv .venv
# setuptools<81: webrtcvad imports pkg_resources, which setuptools 81 removed.
.venv/bin/pip install --quiet --upgrade pip "setuptools<81" wheel
if [ -d "$CONTRACT" ]; then .venv/bin/pip install --quiet -e "$CONTRACT"; else .venv/bin/pip install --quiet le-vogon-contract; fi
.venv/bin/pip install --quiet -e ".[front,dev]"
if [ -z "$_missing" ]; then
    deploy/build_speexdsp.sh .venv/bin/python || echo "    speexdsp build FAILED — step 4 will report aec MISSING"
else
    echo "    skipping the speexdsp build (libraries missing)"
fi

echo "[3/4] env file"
if [ ! -f .env ]; then cp env.example .env; echo "    created .env from env.example — edit it (DEEPGRAM_API_KEY at least)"; else echo "    .env exists"; fi

echo "[4/4] capability check"
.venv/bin/python - <<'PYEOF'
from vogon.gateway_service.capabilities import check_all
failed = False
for cap in check_all():
    print(f"    {cap.name:8s} {'OK' if cap.ok else 'MISSING'}  {cap.detail}")
    failed = failed or not cap.ok
raise SystemExit(1 if failed else 0)
PYEOF
echo "── done. Start with: ./run.sh  (or: .venv/bin/vogon) ─────────────"
