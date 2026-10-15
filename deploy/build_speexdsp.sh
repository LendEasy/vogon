#!/usr/bin/env bash
# Build and install the SpeexDSP echo canceller binding into one Python.
#
#   deploy/build_speexdsp.sh [python]        default: .venv/bin/python
#
# Why a script and not a pip extra: under a PEP 517 build (pip >= 23) setuptools
# runs build_py before build_ext, so the SWIG-generated speexdsp.py is never
# packaged and `import speexdsp` fails with "No module named speexdsp.speexdsp"
# (measured 2026-10-11: the installed package had _speexdsp.so and no
# speexdsp.py). Generating the module in place first fixes the order. The same
# three steps run here, in install.sh, in CI and in the Dockerfile.
#
# Needs the native build libraries first:
#   apt-get install -y git g++ swig libspeex-dev libspeexdsp-dev
set -euo pipefail
PY=${1:-.venv/bin/python}
[ -x "$PY" ] || PY=$(command -v "$PY")
SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT
git clone --quiet --depth 1 https://github.com/xiongyihui/speexdsp-python.git "$SRC/speexdsp-python"
cd "$SRC/speexdsp-python"
"$PY" setup.py --quiet build_ext --inplace
"$PY" -m pip install --quiet --no-cache-dir --no-build-isolation .
"$PY" -c "from speexdsp import EchoCanceller; print('speexdsp: EchoCanceller imports')"
