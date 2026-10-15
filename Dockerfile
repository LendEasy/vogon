# vogon — the voice gateway. Python 3.12 (audioop is in the standard library
# through 3.12; on 3.13 the audioop-lts wheel takes over, see requirements.txt).
FROM python:3.12-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
        git g++ swig libspeex-dev libspeexdsp-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# The build context is this tree alone. The Turn API package (le-vogon-contract) comes from
# a wheel dropped into wheels/ when one is there (a monorepo checkout: `python -m build
# ../vogon-contract -o wheels`), else from CONTRACT_SPEC: its repository until the release is
# on PyPI, then the bare name.
ARG CONTRACT_SPEC="le-vogon-contract @ git+https://github.com/LendEasy/vogon-contract"
COPY . /src/voice-gateway/
# setuptools<81: webrtcvad 2.0.10 imports pkg_resources at import time, which setuptools 81
# removed (measured 2026-10-11 with 84.0.0: the vad capability was MISSING and the gateway
# refused to start). The pin is a runtime need, so it rides in the venv that ships.
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir --upgrade pip "setuptools<81" wheel \
    && if ls /src/voice-gateway/wheels/le_vogon_contract-*.whl >/dev/null 2>&1; \
         then /venv/bin/pip install --no-cache-dir /src/voice-gateway/wheels/le_vogon_contract-*.whl; \
         else /venv/bin/pip install --no-cache-dir "$CONTRACT_SPEC"; fi \
    && /venv/bin/pip install --no-cache-dir "/src/voice-gateway[front]"
# The echo canceller, built in two steps rather than through the `aec` extra: under a
# PEP 517 build (pip ≥ 23) setuptools runs build_py before build_ext, so the SWIG-generated
# speexdsp.py is never packaged and `import speexdsp` fails (measured 2026-10-11: the
# package had _speexdsp.so and no speexdsp.py). Generating it in place first fixes the order;
# the import check makes the build fail here rather than at the gateway's first start.
RUN /src/voice-gateway/deploy/build_speexdsp.sh /venv/bin/python

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libspeexdsp1 libspeex1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 vogon
COPY --from=build /venv /venv
# The reference turn service and the text-call client (GETTING_STARTED.md): the same
# image runs the echo service beside the gateway in docker-compose.yml.
COPY --from=build /src/voice-gateway/examples /opt/vogon/examples
ENV PATH=/venv/bin:$PATH \
    STORAGE_BASE_PATH=/data/storage \
    RECORDINGS_PATH=/data/storage/recordings \
    GATEWAY_PORT=8002
USER vogon
VOLUME ["/data"]
EXPOSE 8002
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/health').status == 200 else 1)"
CMD ["python", "-m", "uvicorn", "vogon.gateway_service.main:app", "--host", "0.0.0.0", "--port", "8002"]
