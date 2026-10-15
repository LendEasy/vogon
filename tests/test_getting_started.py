"""GETTING_STARTED.md, step by step — each step of the guide is a test here, so the
guide cannot drift from what the code does (the table at the end of the guide maps
them). The Python track runs for real: the gateway and the echo turn service on their
own ports in this process, examples/text_call.py as the subprocess a reader runs. The
Docker track's build and run are gated on VOGON_DOCKER_TESTS=1; its static checks
(the compose file against the Dockerfile) always run.

    cd voice-gateway && python -m pytest tests/test_getting_started.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient

from vogon.gateway import settings as gateway_settings
from vogon.gateway_service import routes as gw_routes
from vogon.gateway_service.config import GatewayConfig, gateway_config
from vogon.gateway_service.main import create_app
from vogon.gateway_service.session_store import MemorySessionStore
from vogon_contract import CallConfig, StartCallResponse
from vogon_contract.codec import process_events_response_from_wire, start_call_response_from_wire

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "GETTING_STARTED.md"
EXAMPLES = ROOT / "examples"

# Names the guide may use that are not gateway settings: the examples' own knobs, the
# console script's bind address, this test's gate, and the words of the shell lines.
_GUIDE_NAMES_ALLOWED = {"ECHO_HOST", "ECHO_PORT", "GATEWAY_HOST", "GATEWAY_PORT", "VOGON_DOCKER_TESTS"}


# ── the guide itself ───────────────────────────────────────────────────────────────────

def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    return re.findall(r"```(\w*)\n(.*?)```", text, flags=re.S)


def _settings_env_names() -> set[str]:
    names = {name.upper() for name in gateway_settings.GatewaySettings.model_fields}
    names |= {f"GATEWAY_{name.upper()}" for name in GatewayConfig.model_fields}
    return names


def test_the_guide_names_only_things_that_exist():
    """Every repository path, every example script, every settings name and every test
    name the guide mentions is real."""
    text = GUIDE.read_text()
    for relative in re.findall(r"`((?:examples|deploy|tests)/[\w./-]+|env\.example|README\.md|DEPLOY\.md|CONFIGURATION\.md|docker-compose\.yml)`", text):
        assert (ROOT / relative).exists(), f"the guide names {relative}, which does not exist"
    # the settings the guide tells the reader to set
    named = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})(?:=[^`]*)?`", text))
    unknown = named - _settings_env_names() - _GUIDE_NAMES_ALLOWED
    assert not unknown, f"the guide names settings the gateway does not read: {sorted(unknown)}"
    # the tests the guide's table promises
    own_source = Path(__file__).read_text()
    for test_name in re.findall(r"`(test_[a-z0-9_]+)`", text):
        assert f"def {test_name}(" in own_source, f"the guide promises {test_name}, which is not in this file"
    # the shell lines run the two scripts by their real names
    commands = "\n".join(body for lang, body in _fenced_blocks(text) if lang == "bash")
    assert "python examples/echo_turn_service.py" in commands
    assert "python examples/text_call.py" in commands
    assert "docker compose up --build --wait" in commands and "docker compose down -v" in commands


def test_env_example_loads_as_settings():
    """Python step 2: env.example, copied to .env, is a settings file the gateway reads
    (every key it sets is a known setting; other services' keys are ignored, not fatal)."""
    text = (ROOT / "env.example").read_text()
    keys = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, flags=re.M))
    assert "DEEPGRAM_API_KEY" in keys and "GATEWAY_CAPABILITY_POLICY" in keys
    loaded = gateway_settings.GatewaySettings(_env_file=str(ROOT / "env.example"))
    assert loaded.deepgram_api_key == ""          # the template ships no key
    config = GatewayConfig(_env_file=str(ROOT / "env.example"))
    assert config.capability_policy in ("refuse", "warn")


# ── the echo turn service ──────────────────────────────────────────────────────────────

def _load_example(name: str):
    spec = importlib.util.spec_from_file_location(f"examples_{name}", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_echo_service_implements_the_turn_api():
    """Python step 3: the four endpoints, typed through the contract's codec."""
    echo = _load_example("echo_turn_service")
    with TestClient(echo.app) as client:
        assert client.get("/health").json()["ok"] is True      # what the compose healthcheck probes
        started = client.post("/turn/v1/start_call", json={"call_id": "c1", "mode": "text", "payload": {}})
        assert started.status_code == 200
        opening = start_call_response_from_wire(started.json())
        assert isinstance(opening, StartCallResponse) and isinstance(opening.call_config, CallConfig)
        assert opening.speak and "echo service" in opening.speak[0].text

        replied = client.post("/turn/v1/process_events", json={
            "call_id": "c1", "request_id": "r1",
            "events": [{"kind": "utterance_final", "text": "hello there", "confidence": 1.0, "low_conf_words": []}]})
        reply = process_events_response_from_wire(replied.json())
        assert reply.action == "continue" and reply.speak[0].text == "You said: hello there"

        ended = client.post("/turn/v1/process_events", json={
            "call_id": "c1", "request_id": "r2",
            "events": [{"kind": "utterance_final", "text": "goodbye", "confidence": 1.0, "low_conf_words": []}]})
        farewell = process_events_response_from_wire(ended.json())
        assert farewell.action == "end_call" and farewell.end_reason == "caller_farewell"

        assert client.post("/turn/v1/record_transfer", json={"call_id": "c1", "request_id": "r3", "events": []}).json() == {"appended": 0}
        assert client.post("/turn/v1/end_call", json={"call_id": "c1", "reason": "caller_farewell"}).status_code == 200


# ── the gateway, served for real ───────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Served:
    """An ASGI app on its own port in a daemon thread, stopped at teardown."""

    def __init__(self, app, port: int):
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self.server.started, f"the server on :{self.port} did not start"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """The gateway as step 2 configures it: capability policy warn, no Redis, its
    storage under a temporary directory, the in-process session store."""
    monkeypatch.setenv("GATEWAY_CAPABILITY_POLICY", "warn")
    monkeypatch.setenv("GATEWAY_SESSION_STORE", "memory")
    monkeypatch.setattr(gateway_settings.settings, "redis_enabled", False)
    monkeypatch.setattr(gateway_settings.settings, "storage_base_path", str(tmp_path / "storage"))
    monkeypatch.setattr(gateway_settings.settings, "recordings_path", str(tmp_path / "storage" / "recordings"))
    gateway_config.cache_clear()
    gw_routes.use_store(MemorySessionStore())
    with _Served(create_app(), _free_port()) as served:
        yield served
    gw_routes.use_store(MemorySessionStore())
    gateway_config.cache_clear()


@pytest.fixture
def echo_service():
    with _Served(_load_example("echo_turn_service").app, _free_port()) as served:
        yield served


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def test_health_reports_the_capabilities(gateway):
    """Python step 4: the body the guide shows."""
    health = _get_json(f"http://127.0.0.1:{gateway.port}/health")
    assert health["ok"] is True and health["service"] == "voice-gateway"
    assert set(health["capabilities"]) == {"aec", "vad", "audioop"}


def test_the_text_call_runs_through_a_live_gateway_and_echo_service(gateway, echo_service):
    """Python step 5: the exact command the guide gives, as a subprocess, against a
    gateway and an echo service listening on real ports — the conversation the guide
    prints, line for line, and exit status 0."""
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "text_call.py"),
         "--gateway", f"http://127.0.0.1:{gateway.port}",
         "--turn", f"http://127.0.0.1:{echo_service.port}/turn/v1",
         "--say", "hello there", "--say", "goodbye"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith("call gwc-") and lines[0].endswith(" created")
    assert lines[1].startswith("agent: Hi, this is the echo service.")
    assert lines[2:6] == ["you:   hello there", "agent: You said: hello there", "you:   goodbye", "agent: Goodbye."]
    assert lines[6].startswith("call ended: caller_farewell after ")
    # the gateway's record of the call closed too
    call_id = lines[0].split()[1]
    state = ""
    for _ in range(40):
        state = _get_json(f"http://127.0.0.1:{gateway.port}/v1/tenants/demo/calls/{call_id}")["state"]
        if state == "ended":
            break
        time.sleep(0.1)
    assert state == "ended"


# ── the Docker track ───────────────────────────────────────────────────────────────────

def test_compose_and_dockerfile_agree():
    """The compose file runs the echo service from the path the Dockerfile copies the
    examples to, builds from this directory as the Dockerfile's COPY assumes, and the
    .dockerignore keeps the runtime data, the experiments and the secrets out."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert set(compose["services"]) == {"echo", "vogon"}
    for service in compose["services"].values():
        assert service["build"] == {"context": ".", "dockerfile": "Dockerfile"}
        assert service["image"] == "vogon:local"
    assert "COPY . /src/voice-gateway/" in dockerfile
    assert "le_vogon_contract-*.whl" in dockerfile and 'ARG CONTRACT_SPEC="le-vogon-contract' in dockerfile
    examples_in_image = re.search(r"COPY --from=build /src/voice-gateway/examples (\S+)", dockerfile).group(1)
    assert compose["services"]["echo"]["command"] == ["python", f"{examples_in_image}/echo_turn_service.py"]
    assert compose["services"]["echo"]["environment"]["ECHO_HOST"] == "0.0.0.0"
    assert compose["services"]["vogon"]["environment"]["REDIS_ENABLED"] == "false"
    # the module the image's CMD serves exists and exposes the app
    cmd = json.loads(re.search(r'^CMD (\[.*\])$', dockerfile, flags=re.M).group(1))
    module_name = cmd[cmd.index("uvicorn") + 1].split(":")[0]
    assert hasattr(importlib.import_module(module_name), "app")
    rules = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    for must in ("prototypes/", "storage/", "data/", "tests/", ".env", ".env.*", ".venv/"):
        assert must in rules, must


_DOCKER = shutil.which("docker") is not None


@pytest.mark.skipif(not _DOCKER, reason="docker is not installed")
def test_compose_config_is_valid():
    """Compose accepts the file (interpolation, the env_file `required` form, the anchor)."""
    done = subprocess.run(["docker", "compose", "config", "--quiet"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(not (_DOCKER and os.environ.get("VOGON_DOCKER_TESTS") == "1"),
                    reason="set VOGON_DOCKER_TESTS=1 with docker installed: builds the image (minutes)")
def test_the_docker_track_end_to_end(tmp_path):
    """Docker steps 1–4: build and start, health with every capability true, the same
    conversation from the host against http://echo:9000, tear down with the volume."""
    port = _free_port()
    env = {**os.environ, "GATEWAY_PORT": str(port)}
    project = ["docker", "compose", "-p", "vogon-getting-started-test"]
    log = tmp_path / "compose.log"
    try:
        up = subprocess.run([*project, "up", "--build", "--wait"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=1800)
        log.write_text(up.stdout + up.stderr)
        assert up.returncode == 0, up.stderr[-2000:]
        health = _get_json(f"http://127.0.0.1:{port}/health")
        assert health["ok"] is True and health["capabilities"] == {"aec": True, "vad": True, "audioop": True}
        result = subprocess.run(
            [sys.executable, str(EXAMPLES / "text_call.py"), "--gateway", f"http://127.0.0.1:{port}",
             "--turn", "http://echo:9000/turn/v1", "--say", "hello there", "--say", "goodbye"],
            capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert "agent: You said: hello there" in result.stdout and "call ended: caller_farewell" in result.stdout
    finally:
        # the containers' own output, kept beside the compose log for a failed run
        logs = subprocess.run([*project, "logs", "--no-color"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        (tmp_path / "containers.log").write_text(logs.stdout + logs.stderr)
        subprocess.run([*project, "down", "-v"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
