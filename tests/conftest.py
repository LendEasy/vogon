"""The gateway's test configuration — the tree runs from its own root.

    cd voice-gateway && python -m pytest tests -q

Path: the package (`vogon`) is this tree's root; the Turn API (`vogon_contract`)
is the sibling vogon-contract/src until it is an installed dependency.
Hermetic: the session store is in-process (no Redis), and no test may reach a
paid decisions call (Jev) or a paid speech render (Deepgram) — the same two
guards the application's test suite carries, for the gateway's own clients.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT.parent / "vogon-contract" / "src", Path(__file__).resolve().parent):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from vogon.gateway import jev as _jev  # noqa: E402 — after the path wiring
from vogon.gateway.settings import settings as _settings  # noqa: E402

_settings.redis_enabled = False

_REAL_CALL_MARKERS = ("live", "corpus", "pivot", "bench")


def pytest_configure(config):
    for marker in _REAL_CALL_MARKERS:
        config.addinivalue_line("markers", f"{marker}: may make real network calls")


@pytest.fixture(autouse=True)
def _no_real_jev_calls(request, monkeypatch):
    """An offline test that reaches the gateway's Jev client would make a real, paid
    decisions call — and the front model turns every Jev error into "say nothing", so
    such a test could PASS on the fallback. Block the SDK; fail at teardown if tried."""
    if any(request.node.get_closest_marker(m) for m in _REAL_CALL_MARKERS):
        yield
        return
    attempts: list[str] = []

    class _BlockedOpenRouter:
        def __init__(self, *a, **k):
            attempts.append(request.node.nodeid)
            raise RuntimeError("offline test reached a real Jev decisions call")

    monkeypatch.setattr(_jev, "_client", None)
    try:
        import openrouter  # noqa: PLC0415 — optional: the except branch handles its absence
        monkeypatch.setattr(openrouter, "OpenRouter", _BlockedOpenRouter)
    except ImportError:
        pass
    yield
    if attempts:
        pytest.fail(f"{len(attempts)} real Jev decisions call(s) attempted in an offline test — "
                    f"fake vogon.gateway.jev.decide_async (see test_gateway_jev.py)", pytrace=False)


@pytest.fixture(autouse=True)
def _no_real_tts_renders(request, monkeypatch):
    """The warm transfer renders its lines through Deepgram's REST speak and falls back
    to Twilio <Say> on failure — a test could pass on a real (paid) call's failure."""
    if any(request.node.get_closest_marker(m) for m in _REAL_CALL_MARKERS):
        yield
        return
    from vogon.gateway_service.escalation import voice  # noqa: PLC0415
    attempts: list[str] = []

    async def _blocked(text, voice_name):
        attempts.append(request.node.nodeid)
        raise RuntimeError("offline test reached a real Deepgram render")
    monkeypatch.setattr(voice, "_render_one", _blocked)
    yield
    if attempts:
        pytest.fail(f"{len(attempts)} real Deepgram render(s) attempted in an offline test — "
                    f"fake vogon.gateway_service.escalation.voice._render_one", pytrace=False)
