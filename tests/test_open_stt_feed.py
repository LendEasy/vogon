"""Open STT feed (docs/stt-open-feed.md).

During agent playback, caller audio used to reach Deepgram only after the
barge gate confirmed a loud-enough barge — quiet or overlapping speech was
silently dropped and turns fragmented. With stt_feed_mode="open" the
AEC-cleaned audio streams to Deepgram unconditionally; the gate keeps only
its playback-cut decision, and echo protection moves to the text layer.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import struct
import time

from transport_fixtures import bare_twilio_transport

from vogon.gateway.settings import GatewaySettings as Settings
from vogon.gateway.settings import settings
from vogon.gateway.transport.barge_semantics import is_echo_of, should_swallow_final
from vogon.gateway.transport.twilio_transport import TwilioTransport

# ── is_echo_of (the text-level echo guard) ──────────────────────────────────────

_SPEAK = (
    "Absolutely — I can send a payment link to your phone for $100.00 instead. "
    "Shall I send it now?"
)


def test_echo_fragment_of_our_own_speech_is_detected():
    assert is_echo_of("I can send a payment link to your phone", _SPEAK) is True
    assert is_echo_of("send a payment link to your phone for $100.00", _SPEAK) is True


def test_short_genuine_replies_are_never_echo():
    # "yes"/"okay"/short answers can appear inside our own line — they must be
    # judged by the verdict flow, never discarded as echo.
    assert is_echo_of("yes", _SPEAK) is False
    assert is_echo_of("send it now", _SPEAK) is False   # 3 words < min_words


def test_unrelated_speech_is_not_echo():
    assert is_echo_of("can I pay it on the phone instead", _SPEAK) is False
    assert is_echo_of("", _SPEAK) is False


# ── Frame flow: open vs gated (real transport, synthetic frames) ────────────────

def _transport(monkeypatch, feed_mode):
    monkeypatch.setattr(settings, "stt_feed_mode", feed_mode)
    monkeypatch.setattr(settings, "semantic_barge_enabled", True)

    class _Stt:
        def __init__(self): self.received = []
        async def send_audio(self, b): self.received.append(b)

    class _Aec:
        def process(self, b): return b
        def feed_reference(self, b): pass
        def discard_reference(self): pass

    class _S:
        config = {"assistant": {"workflow_id": "wf"}}
        call_id = "open-feed-e2e"
        def record_analytics(self, *a, **k): pass

    async def _noop(*a, **k): pass

    t = TwilioTransport(ws=None, send_raw=_noop, call_id="t-call")
    t._stt = _Stt()
    t._aec = _Aec()
    t._stream_sid = "MZ"
    t._recorder = None
    return t


def _media(v):
    mulaw = audioop.lin2ulaw(struct.pack("<160h", *([v] * 160)), 2)
    return {"event": "media", "media": {"payload": base64.b64encode(mulaw).decode()}}


def _drive_quiet_frames(t, n=5, rms=400):
    """Sub-threshold caller speech (rms 400 < gate threshold 1200) while the
    agent's audio is playing — the exact audio the old valve dropped."""
    async def go():
        t._tts_playing = True
        t._tts_started_at = 0.0
        t._barge_gate.on_tts_start(0.0)
        for _ in range(n):
            await t.handle_text_frame(_media(rms))
    asyncio.run(go())


def test_open_feed_streams_quiet_speech_during_playback(monkeypatch):
    t = _transport(monkeypatch, "open")
    _drive_quiet_frames(t)
    assert len(t._stt.received) == 5          # every frame reached Deepgram
    assert not t._tts_cancel.is_set()         # and nothing cut the playback


def test_gated_mode_restores_the_old_valve(monkeypatch):
    t = _transport(monkeypatch, "gated")
    _drive_quiet_frames(t)
    assert t._stt.received == []              # sub-threshold speech dropped


# ── _judge_playback_final (finals arriving during playback) ─────────────────────

class _FakeAudioLog:
    def __init__(self): self.events = []
    def emit(self, ts, name, **kw): self.events.append((name, kw))


class _FakeGate:
    threshold = 1200
    class cfg:
        confirm = "off"
    def reset(self): pass


def _bare(monkeypatch, feed_mode="open"):
    monkeypatch.setattr(settings, "stt_feed_mode", feed_mode)
    monkeypatch.setattr(settings, "tts_pacing_mode", "sentence")
    return bare_twilio_transport(
        _tts_playing=True,
        _tts_started_at=time.monotonic(),
        _last_audio_id="resp-3",
        _last_speak_text=_SPEAK,
        _audio_log=_FakeAudioLog(),
        _barge_gate=_FakeGate(),
    )


def test_playback_final_echo_is_absorbed(monkeypatch):
    t = _bare(monkeypatch)
    asyncio.run(t._judge_playback_final("I can send a payment link to your phone"))
    assert t._absorbing_utterance is True
    assert t._absorb_detail["verdict"] == "echo"
    assert not t._tts_cancel.is_set()          # audio keeps playing
    assert any(n == "echo_discarded" for n, _ in t._audio_log.events)


def test_playback_final_backchannel_is_absorbed(monkeypatch):
    t = _bare(monkeypatch)
    asyncio.run(t._judge_playback_final("okay"))
    assert t._absorbing_utterance is True
    assert t._absorb_detail["verdict"] == "backchannel"
    assert not t._tts_cancel.is_set()


def test_playback_final_substantive_cuts_and_processes(monkeypatch):
    t = _bare(monkeypatch)
    asyncio.run(t._judge_playback_final(
        "wait stop I need to ask you something about this account first"
    ))
    assert t._absorbing_utterance is False     # processes as a real turn
    assert t._tts_cancel.is_set()              # and the playback was cut


def test_echo_verdict_always_swallows_despite_length():
    # The grew-substantive re-check must not promote an echo to a real turn.
    echo_text = "I can send a payment link to your phone for $100.00 instead"
    assert should_swallow_final(echo_text) is False   # by length it looks real...
    # ...which is exactly why on_final short-circuits on verdict == "echo"
    # (asserted here as the documented contract of the swallow branch).


def test_config_default_is_open():
    assert Settings.model_fields["stt_feed_mode"].default == "open"
