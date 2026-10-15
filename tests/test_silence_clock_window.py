"""The silence window measures how long the CALLER has been quiet — not how
long we spent thinking and talking.

CALL c51a075a, from journald. The arithmetic is exact and worth keeping:

    18:23:31.163  REPLY turn=5 | words=126
    18:24:35.847  TTS error (1011) | sentences=4/6 frames=3256 audio_ms=65120
    18:24:40.057  Silence detected | level=warn  silent_for=8.0s
    18:24:42.057  Silence detected | level=alert silent_for=10.0s — firing probe

warn at 18:24:40.06 having counted 8.0s puts the window's start at 18:24:32.06
— 3.8 seconds BEFORE the TTS stream ended, and while ~65 seconds of audio was
buffered at Twilio still playing to the caller. Two separate causes, both here:

  1. The window was never restarted when a turn was spoken, so the gap between
     the caller's last word and the agent's first — the LLM turn — was counted
     as the caller being silent. That is the 3.8s.
  2. A stream that FAILED mid-way was treated as "no buffered audio in Twilio,
     start the silence timer immediately". 3256 frames had already been handed
     to Twilio. The caller was still listening to them when we asked whether
     they were still there.

The probe exception is what keeps the ladder honest: our own probe must not
restart the window, or the exit threshold never arrives and a call the caller
has walked away from is never hung up.

    cd voice-gateway
    python -m pytest tests/test_silence_clock_window.py -v
"""
from __future__ import annotations

import inspect

import pytest

from vogon.gateway.transport.clocks import CallClocks
from vogon.gateway.transport.twilio_transport import TwilioTransport


class _Clock:
    """Just the silence bookkeeping, driven on a fake timeline."""

    def __init__(self):
        self.c = CallClocks.__new__(CallClocks)
        self.c._sil_idle = 0.0
        self.c._sil_fired = set()
        self.c._sil_armed = True
        # A playout DEPTH, not a flag: a front line and the desk's answer can
        # be in flight together, and the front line's end must not un-pause
        # the clock while the desk is still talking.
        self.c._playing_depth = 0
        self.c._playing = False
        self.c._playing_since = None
        self.c._playout_audio_ms = None

    def idle(self, seconds: float):
        """Advance the timeline while nobody is speaking."""
        if self.c._sil_armed and not self.c._playing:
            self.c._sil_idle += seconds


class TestARealTurnRestartsTheWindow:
    def test_thinking_time_is_not_charged_to_the_caller(self):
        k = _Clock()
        k.idle(3.8)                       # the LLM turn — c51a075a's 3.8s
        k.c.note_playout_start()
        k.c.note_playout_end(reset_window=True)
        assert k.c._sil_idle == 0.0, (
            "the caller's silence starts when we STOP talking, not when they "
            f"did — {k.c._sil_idle}s carried over")

    def test_the_caller_gets_the_whole_window_after_a_turn(self):
        k = _Clock()
        k.idle(3.8)
        k.c.note_playout_end(reset_window=True)
        k.idle(7.9)
        assert k.c._sil_idle < 8.0, "warn fired early on carried-over idle"

    def test_a_fired_threshold_is_rearmed_by_a_turn(self):
        # Otherwise the next window skips straight past warn.
        k = _Clock()
        k.idle(11.0)
        k.c._sil_fired.update({"warn", "alert"})
        k.c.note_playout_end(reset_window=True)
        assert k.c._sil_fired == set()


class TestTheProbeDoesNotRestartIt:
    """The exit threshold is the only thing that hangs up a dead call."""

    def test_a_probe_leaves_the_window_running(self):
        k = _Clock()
        k.idle(10.0)                      # warn + alert have fired; probe spoken
        k.c._sil_fired.update({"warn", "alert"})
        k.c.note_playout_end()            # default: no reset
        k.idle(27.0)
        assert k.c._sil_idle >= 37.0, "exit can never arrive"
        assert "exit" not in k.c._sil_fired  # not yet evaluated, but reachable

    def test_the_default_is_no_reset(self):
        k = _Clock()
        k.idle(5.0)
        k.c.note_playout_end()
        assert k.c._sil_idle == 5.0


class TestWhichAudioCountsAsATurn:
    @pytest.mark.parametrize("audio_id", ["resp-6", "resp-12", "greeting-1"])
    def test_agent_turns_restart_the_window(self, audio_id):
        assert TwilioTransport._is_turn_audio(audio_id) is True

    @pytest.mark.parametrize("audio_id", ["silence-probe-7", "silence-exit-9"])
    def test_the_ladders_own_speech_does_not(self, audio_id):
        assert TwilioTransport._is_turn_audio(audio_id) is False

    def test_a_missing_id_is_treated_as_a_turn(self):
        # Failing open here restarts the window, which is the safe direction:
        # the wrong call is asking "are you still there?" over our own voice.
        assert TwilioTransport._is_turn_audio("") is True
        assert TwilioTransport._is_turn_audio(None) is True


class TestBufferedAudioIsNotSilence:
    """A stream that dies mid-way leaves what it already sent playing."""

    def test_frames_already_sent_keep_the_clock_paused(self):
        src = inspect.getsource(TwilioTransport)
        # The failure path must hand the outstanding audio to the watchdog
        # rather than declaring the playout over.
        at = src.index("FAILED MID-STREAM")
        tail = src[at:at + 1400]
        assert "note_playout_sent" in tail
        assert "_note_playout_end()" not in tail, \
            "ending the playout here starts the timer over live audio"

    def test_nothing_sent_still_starts_the_timer_immediately(self):
        src = inspect.getsource(TwilioTransport)
        at = src.index("Cancelled or failed before any audio reached Twilio")
        assert "_note_playout_end(" in src[at:at + 400]
