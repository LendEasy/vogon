"""Barge-in must stay armed through the PLAYBACK-DRAIN window.

Root cause fixed here: the send loop runs ahead of real time, so the TTS
stream finishes sending seconds before Twilio finishes playing. The old gate
condition (`_tts_playing or cooldown`) went dark the moment sending ended —
a caller barge mid-reply left the buffered audio running to the end. The
drain window (mark sent, echo not yet received) now keeps detection armed,
and a confirmed drain barge clears Twilio's buffer and ends the drain state.
"""
from __future__ import annotations

import asyncio
import json
import time

from transport_fixtures import bare_twilio_transport

from vogon.gateway.transport.twilio_transport import (
    _DRAIN_WINDOW_MAX_SEC,
    _TTS_ECHO_COOLDOWN_SEC,
)
from vogon_contract import DrainCleared


def _bare(marks=None, last_audio_id="resp-9"):
    """TwilioTransport with drain-window state under test."""
    return bare_twilio_transport(
        _pending_marks=marks if marks is not None else {},
        _last_audio_id=last_audio_id,
        _stream_sid="MZ123",
    )


class _FakeAudioLog:
    def __init__(self):
        self.events = []

    def emit(self, ts, name, **kw):
        self.events.append((name, kw))


# ── _playback_draining ──────────────────────────────────────────────────────────

def test_not_draining_with_no_pending_marks():
    # Call start / after echo: nothing pending → normal listening, gate off.
    assert _bare(marks={})._playback_draining is False


def test_draining_while_mark_unechoed():
    t = _bare(marks={"tts_end_resp-9": (time.monotonic(), "snippet")})
    assert t._playback_draining is True


def test_stale_mark_does_not_pin_the_gate_open():
    # A lost echo must not leave the gate in drain mode forever.
    stale = time.monotonic() - (_DRAIN_WINDOW_MAX_SEC + 5.0)
    t = _bare(marks={"tts_end_resp-9": (stale, "snippet")})
    assert t._playback_draining is False


def test_drain_cap_generous_enough_for_long_replies():
    # The cap guards lost echoes, not real replies — must exceed any
    # plausible playback duration.
    assert _DRAIN_WINDOW_MAX_SEC >= 60.0
    assert _TTS_ECHO_COOLDOWN_SEC < 1.0  # cooldown stays a short echo guard


# ── _clear_buffered_playback (the drain barge action) ───────────────────────────

def test_drain_clear_discards_buffer_and_ends_drain_state():
    t = _bare(marks={"tts_end_resp-9": (time.monotonic(), "snippet")})
    t._audio_log = _FakeAudioLog()
    sent = []

    async def _fake_send_raw(raw):
        sent.append(json.loads(raw))

    t._send_raw = _fake_send_raw

    before = time.monotonic()
    asyncio.run(t._clear_buffered_playback())

    # Twilio told to discard its buffered audio.
    assert sent == [{"event": "clear", "streamSid": "MZ123"}]
    # Late truncation pushed as a DrainCleared event (phase0 §4.2 step 4).
    _events = []
    while not t._utterance_queue.empty():
        _events.append(t._utterance_queue.get_nowait())
    assert any(
        isinstance(e, DrainCleared) and e.audio_id == "resp-9" for e in _events
    )
    # Drain state ended: no pending marks → _playback_draining False,
    # and the silence anchor is the actual stop time (echo cooldown takes over).
    assert t._pending_marks == {}
    assert t._playback_draining is False
    assert t._tts_ended_at >= before


def test_drain_clear_is_noop_send_without_stream_sid():
    # Defensive: no streamSid → no clear frame, but drain state still ends.
    t = _bare(marks={"tts_end_resp-9": (time.monotonic(), "s")})
    t._stream_sid = ""
    t._audio_log = _FakeAudioLog()
    sent = []

    async def _fake_send_raw(raw):
        sent.append(raw)

    t._send_raw = _fake_send_raw
    asyncio.run(t._clear_buffered_playback())
    assert sent == []
    assert t._pending_marks == {}
