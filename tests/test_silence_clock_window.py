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
from test_call_clocks import SILENCE, T0, make_clocks, run_ticks

from vogon.gateway.transport.twilio_transport import TwilioTransport


def _turn(reset: bool):
    """An earlier turn has armed the window; the caller answers and we think for 4 s, speak for
    2 s, and stop — driven through the real CallClocks.tick() at 1 Hz. Until 2026-10-30 these
    tests advanced a test-owned idle counter and no threshold was ever evaluated."""
    clocks, events = make_clocks(silence=SILENCE)
    clocks.note_playout_end()                      # an earlier turn: the window is armed
    run_ticks(clocks, 1, 5)                        # 4 s: the LLM turn (c51a075a's 3.8 s)
    clocks.note_playout_start(is_turn_audio=True)
    run_ticks(clocks, 5, 7)                        # 2 s of our speech
    clocks.note_playout_end(reset_window=reset)    # the turn stops playing at ~T0+6
    return clocks, events


class TestARealTurnRestartsTheWindow:
    def test_thinking_time_is_not_charged_to_the_caller(self):
        clocks, events = _turn(reset=True)
        run_ticks(clocks, 7, 30)
        # the caller gets a whole 8 s after we stop talking: warn at T0+14. Without the reset the
        # 4 s of thinking was charged to them and warn came at T0+10 (measured).
        assert [(e.threshold_id, e.at - T0) for e in events][:1] == [("warn", 14.0)]

    def test_a_fired_threshold_is_rearmed_by_a_turn(self):
        # Otherwise the next window skips straight past warn.
        clocks, events = make_clocks(silence=SILENCE)
        clocks.note_playout_end()
        run_ticks(clocks, 1, 12)                   # warn@8, alert@10
        clocks.note_playout_start(is_turn_audio=True)
        clocks.note_playout_end(reset_window=True)
        run_ticks(clocks, 12, 21)
        assert [e.threshold_id for e in events] == ["warn", "alert", "warn"]


# The probe half — our own probe must NOT restart the window, or exit never arrives — is pinned
# on the real tick in test_call_clocks.py (test_playback_pauses_idle_clock_without_reset,
# test_watchdog_on_a_probe_does_not_reset_the_window).


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
