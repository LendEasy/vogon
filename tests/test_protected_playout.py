"""Intro/farewell protected playouts (barge-in-compliance-protection, slice).

A protected playout (the call's first utterance — carries the REQ-CALL-02
recorded-line disclosure — and any call-ending utterance) plays to completion:
- safety phrases still cut immediately;
- backchannels/noise absorb as usual;
- substantive speech is DEFERRED — neither cut nor swallowed; it queues and is
  answered right after the playout finishes;
- no Cerebras verdict call is made while protected.
"""
from __future__ import annotations

import asyncio
import inspect
import time

from transport_fixtures import bare_twilio_transport

from vogon.gateway.settings import settings
from vogon.gateway.transport.barge_semantics import should_swallow_absorbed_final
from vogon.gateway.transport.base import BaseTransport
from vogon.gateway.transport.text_transport import TextTransport
from vogon.gateway.transport.twilio_transport import TwilioTransport
from vogon.gateway.transport.voice_transport import VoiceTransport


class _FakeAudioLog:
    def __init__(self): self.events = []
    def emit(self, ts, name, **kw): self.events.append((name, kw))


class _FakeGate:
    threshold = 1200
    class cfg:
        confirm = "off"
    def reset(self): pass


def _bare(monkeypatch, protected=True):
    monkeypatch.setattr(settings, "tts_pacing_mode", "sentence")
    return bare_twilio_transport(
        _protected_playout=protected,
        _tts_playing=True,
        _tts_started_at=time.monotonic(),
        _last_audio_id="open-001",
        _last_speak_text=(
            "Hi, this is Kaya calling on a recorded line — this is a personal "
            "business call regarding account servicing. May I speak with Deep Kumar?"
        ),
        _audio_log=_FakeAudioLog(),
        _barge_gate=_FakeGate(),
    )


def _events(t):
    return [n for n, _ in t._audio_log.events]


def test_send_response_signatures_accept_protected():
    for cls in (BaseTransport, TwilioTransport, VoiceTransport, TextTransport):
        params = inspect.signature(cls.send_response).parameters
        assert "protected" in params, cls.__name__
        assert params["protected"].default is False, cls.__name__


# ── _act_on_barge_verdict guard (defense in depth) ──────────────────────────────

def test_protected_substantive_verdict_never_cuts(monkeypatch):
    t = _bare(monkeypatch)
    asyncio.run(t._act_on_barge_verdict(
        "substantive_interrupt", "llm", "wait I have a question", time.monotonic(),
    ))
    assert not t._tts_cancel.is_set()
    assert not t._graceful_yield
    assert "barge_deferred_protected" in _events(t)


def test_protected_safety_still_cuts(monkeypatch):
    t = _bare(monkeypatch)
    asyncio.run(t._act_on_barge_verdict(
        "safety", "heuristic", "stop calling me", time.monotonic(),
    ))
    assert t._tts_cancel.is_set()          # the break-through exception


def test_protected_backchannel_still_absorbs(monkeypatch):
    t = _bare(monkeypatch)
    asyncio.run(t._act_on_barge_verdict(
        "backchannel", "heuristic", "okay", time.monotonic(),
    ))
    assert t._absorbing_utterance is True  # swallowed as usual
    assert not t._tts_cancel.is_set()


def test_unprotected_behavior_unchanged(monkeypatch):
    t = _bare(monkeypatch, protected=False)
    asyncio.run(t._act_on_barge_verdict(
        "substantive_interrupt", "heuristic", "wait a second here", time.monotonic(),
    ))
    assert t._tts_cancel.is_set()          # normal cut still works


# ── _judge_playback_final under protection ──────────────────────────────────────

def test_protected_final_substantive_defers(monkeypatch):
    t = _bare(monkeypatch)
    asyncio.run(t._judge_playback_final(
        "wait who is this and why are you calling my number today"
    ))
    assert not t._tts_cancel.is_set()      # no cut
    assert t._absorbing_utterance is False # NOT swallowed — will queue + process
    assert "barge_deferred_protected" in _events(t)


def test_protected_final_backchannel_absorbs(monkeypatch):
    t = _bare(monkeypatch)
    asyncio.run(t._judge_playback_final("okay"))
    assert t._absorbing_utterance is True
    assert not t._tts_cancel.is_set()


def test_protected_final_safety_cuts(monkeypatch):
    t = _bare(monkeypatch)
    asyncio.run(t._judge_playback_final("stop calling me right now"))
    assert t._tts_cancel.is_set()


def test_protected_final_echo_still_discarded(monkeypatch):
    # The echo guard precedes the protected branch and must keep working.
    t = _bare(monkeypatch)
    asyncio.run(t._judge_playback_final(
        "calling on a recorded line — this is a personal business call"
    ))
    assert t._absorbing_utterance is True
    assert t._absorb_detail["verdict"] == "echo"


# ── The protected window (energy-confirm path) ──────────────────────────────────

def test_protected_window_defers_substantive(monkeypatch):
    monkeypatch.setattr(settings, "semantic_barge_window_ms", 200)
    t = _bare(monkeypatch)
    t._last_interim_text = "hold on I want to ask you something about this call"
    asyncio.run(t._run_protected_window())
    assert not t._tts_cancel.is_set()
    assert t._absorbing_utterance is False
    assert "barge_deferred_protected" in _events(t)


def test_protected_window_safety_cuts(monkeypatch):
    monkeypatch.setattr(settings, "semantic_barge_window_ms", 200)
    t = _bare(monkeypatch)
    t._last_interim_text = "stop calling me"
    asyncio.run(t._run_protected_window())
    assert t._tts_cancel.is_set()


def test_protected_window_noise_absorbs_on_expiry(monkeypatch):
    monkeypatch.setattr(settings, "semantic_barge_window_ms", 150)
    t = _bare(monkeypatch)
    t._last_interim_text = ""              # energy confirmed, no words (cough)
    asyncio.run(t._run_protected_window())
    assert not t._tts_cancel.is_set()
    assert t._absorbing_utterance is True  # absorbed as noise
    assert t._absorb_detail["verdict"] == "no_text"


# ── Absorb-all playouts — the opening (2026-08-02) ──────────────────────────
# During the opening every verdict but `safety` absorbs: nothing queues
# behind the greeting. Three doors could re-queue speech; each is pinned.


def _bare_intro(monkeypatch):
    t = _bare(monkeypatch)
    t._absorb_playout = True
    return t


def test_intro_window_absorbs_substantive(monkeypatch):
    """Door 1: the energy-confirm window. Substantive speech over the opening
    is ABSORBED (was: deferred → queued → answered after the greeting)."""
    monkeypatch.setattr(settings, "semantic_barge_window_ms", 200)
    t = _bare_intro(monkeypatch)
    t._last_interim_text = "hold on I want to ask you something about this call"
    asyncio.run(t._run_protected_window())
    assert not t._tts_cancel.is_set(), "intro must keep playing"
    assert t._absorbing_utterance is True
    assert t._absorb_detail["verdict"] == "intro_absorb"
    assert "barge_deferred_protected" not in _events(t)


def test_intro_window_safety_still_cuts(monkeypatch):
    """Safety is the one verdict absorb-all does not override."""
    monkeypatch.setattr(settings, "semantic_barge_window_ms", 200)
    t = _bare_intro(monkeypatch)
    t._last_interim_text = "stop calling me"
    asyncio.run(t._run_protected_window())
    assert t._tts_cancel.is_set()


def test_intro_playback_final_absorbs_quiet_talker(monkeypatch):
    """Door 2: a final that never tripped the energy gate (open STT feed)
    absorbs too, instead of deferring to the queue."""
    t = _bare_intro(monkeypatch)
    asyncio.run(t._judge_playback_final(
        "yeah so I was calling about the balance on my account"))
    assert t._absorbing_utterance is True
    assert t._absorb_detail["verdict"] == "intro_absorb"
    assert not t._tts_cancel.is_set()


def test_intro_absorbed_final_swallows_even_grown_substantive():
    """Door 3: should_swallow_final un-swallows a final that grew substantive
    — correct mid-call, wrong during the opening. intro_absorb swallows
    unconditionally, so the final arrives TAGGED and can never drive a turn."""

    grown = "yeah so I want to talk about my account balance and the fees"
    # Mid-call absorb: the grown final is promoted to a real turn (unchanged).
    assert should_swallow_absorbed_final("backchannel", grown) is False
    # Opening absorb: swallowed regardless — nothing queues behind the greeting.
    assert should_swallow_absorbed_final("intro_absorb", grown) is True
    # Echo keeps its unconditional swallow.
    assert should_swallow_absorbed_final("echo", grown) is True


def test_farewell_window_still_defers_substantive(monkeypatch):
    """The farewell keeps today's defer semantics — absorb-all is the
    OPENING's policy, declared per-command, not a protected-window default."""
    monkeypatch.setattr(settings, "semantic_barge_window_ms", 200)
    t = _bare(monkeypatch)          # protected, absorb_playout False
    t._last_interim_text = "hold on I want to ask you something about this call"
    asyncio.run(t._run_protected_window())
    assert t._absorbing_utterance is False
    assert "barge_deferred_protected" in _events(t)

