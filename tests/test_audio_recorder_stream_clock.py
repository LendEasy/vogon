"""Inbound frames are placed by Twilio's stream clock, not by arrival time.

A frame's stream timestamp says when the audio was SPOKEN. Its arrival time is
that plus a variable network delay, so positioning by arrival scatters frames
by however long each one took to reach us — up to 17 ms of a 20 ms frame,
audible as roughness while the caller speaks. Measured against Twilio's own
recording of the same call, our caller leg correlated 0.97 at best and 0.67 on
average, while the agent leg — already on a sample clock — matched exactly:
every frame present and correctly decoded, just in slightly the wrong places.

These tests feed frames whose ARRIVAL times are jittered but whose stream
timestamps are exact, and assert the recording comes out evenly spaced. They
also pin the two things the placement anchor can and cannot absorb, and the
separate question of where the timeline starts.
"""
from __future__ import annotations

import audioop
import io
import wave

import numpy as np
import pytest

from vogon.gateway.audio.audio_recorder import _MAX_DRIFT_SEC, AudioRecorder, _build_timeline

FRAME_MS = 20
FRAME_SAMPLES = 160          # 20 ms at 8 kHz, one Twilio media frame


def _tone_frame(seq: int) -> bytes:
    """A distinct 20 ms mulaw frame — a ramp seeded by sequence number, so a
    misplaced frame is detectable by value, not just by silence."""
    t = np.arange(seq * FRAME_SAMPLES, (seq + 1) * FRAME_SAMPLES)
    pcm = (8000 * np.sin(2 * np.pi * 440 * t / 8000)).astype(np.int16)
    return audioop.lin2ulaw(pcm.tobytes(), 2)


def _decode(wav: bytes) -> tuple[np.ndarray, np.ndarray]:
    with wave.open(io.BytesIO(wav), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a[0::2], a[1::2]          # caller (L), agent (R)


def _assert_frame_at(channel: np.ndarray, seq: int, sample_offset: int) -> None:
    """Assert frame `seq` occupies exactly [sample_offset, +160).

    Not `flatnonzero`: _tone_frame(0) opens on sin(0) == 0, so the first
    non-zero sample is one past the frame's true start.
    """
    expected = np.frombuffer(audioop.ulaw2lin(_tone_frame(seq), 2), dtype=np.int16)
    block = channel[sample_offset : sample_offset + FRAME_SAMPLES]
    assert np.array_equal(block, expected), f"frame {seq} is not at sample {sample_offset}"


def _feed(rec: AudioRecorder, arrivals_ms: list[float], stamped: bool) -> None:
    """Feed N frames whose stream timestamps are exact and whose arrival times
    are whatever `arrivals_ms` says."""
    for seq, arrival_ms in enumerate(arrivals_ms):
        media = {"timestamp": str(seq * FRAME_MS)} if stamped else {}
        ts = rec.caller_timestamp(media, arrival_ms / 1000.0)
        rec.add_caller_chunk(ts, _tone_frame(seq))


# ── the fix ───────────────────────────────────────────────────────────────────

def test_jittered_arrivals_are_placed_on_an_even_grid():
    """The core guarantee: frames land exactly one frame apart regardless of
    how erratically they arrived."""
    n = 50
    rng = np.random.default_rng(7)
    jitter = rng.uniform(-9.0, 17.0, n)          # the range measured in prod
    arrivals = [i * FRAME_MS + j for i, j in enumerate(jitter)]

    rec = AudioRecorder()
    _feed(rec, arrivals, stamped=True)
    caller, _ = _decode(rec.to_wav_bytes())

    # Reconstruct what a jitter-free capture would be, and compare.
    expected = np.frombuffer(
        audioop.ulaw2lin(b"".join(_tone_frame(i) for i in range(n)), 2), dtype=np.int16,
    )
    assert len(caller) >= len(expected)
    got = caller[: len(expected)].astype(float)
    exp = expected.astype(float)
    r = np.corrcoef(got, exp)[0, 1]
    assert r > 0.999, f"stream-clock placement should be sample-exact, got r={r:.4f}"


def test_arrival_time_placement_is_what_jitter_used_to_corrupt():
    """The control that gives the test above its meaning: the SAME jittered
    arrivals, placed the old way, do NOT reconstruct the signal."""
    n = 50
    rng = np.random.default_rng(7)
    jitter = rng.uniform(-9.0, 17.0, n)
    arrivals = [i * FRAME_MS + j for i, j in enumerate(jitter)]

    rec = AudioRecorder()
    for seq, arrival_ms in enumerate(arrivals):
        rec.add_caller_chunk(arrival_ms / 1000.0, _tone_frame(seq))   # old behaviour
    caller, _ = _decode(rec.to_wav_bytes())

    expected = np.frombuffer(
        audioop.ulaw2lin(b"".join(_tone_frame(i) for i in range(n)), 2), dtype=np.int16,
    )
    got = caller[: len(expected)].astype(float)
    r = np.corrcoef(got, expected.astype(float))[0, 1]
    assert r < 0.99, (
        f"arrival-time placement should be degraded by jitter (that is the bug); "
        f"got r={r:.4f} — if this passes cleanly the test no longer proves anything"
    )


def test_frames_are_evenly_spaced_even_when_arrivals_bunch():
    """A burst — several frames delivered in the same instant — must not stack
    them on top of each other. Under arrival-time placement they overwrote one
    another, destroying audio with no silence to show for it."""
    arrivals = [0.0, 1.0, 2.0, 3.0, 4.0, 100.0, 101.0]     # 5 frames in one burst
    rec = AudioRecorder()
    _feed(rec, arrivals, stamped=True)
    caller, _ = _decode(rec.to_wav_bytes())

    for seq in range(len(arrivals)):
        start = seq * FRAME_SAMPLES
        block = caller[start : start + FRAME_SAMPLES]
        expected = np.frombuffer(audioop.ulaw2lin(_tone_frame(seq), 2), dtype=np.int16)
        assert np.array_equal(block, expected), f"frame {seq} is not at its own offset"


# ── robustness ────────────────────────────────────────────────────────────────

def test_missing_timestamp_falls_back_to_arrival_and_is_counted():
    rec = AudioRecorder()
    assert rec.caller_timestamp({}, 12.5) == 12.5
    assert rec.caller_timestamp({"timestamp": None}, 3.0) == 3.0
    assert rec.caller_timestamp({"timestamp": "not-a-number"}, 4.0) == 4.0
    assert rec._stream_ts_missing == 3
    assert rec._stream_ts_used == 0


def test_anchor_keeps_both_legs_in_one_timebase():
    """to_wav_bytes places both legs against a single shared t0, so the caller
    leg must stay in the monotonic domain — the stream clock supplies an
    offset from the anchor, never an absolute."""
    rec = AudioRecorder()
    first = rec.caller_timestamp({"timestamp": "5000"}, 100.0)
    assert first == 100.0, "the first stamped frame anchors to its arrival time"
    # inside the drift window, stream time alone decides the position
    later = rec.caller_timestamp({"timestamp": "5100"}, 100.12)
    assert later == pytest.approx(100.1), "later frames advance by STREAM time"


def test_a_frame_far_from_its_arrival_re_anchors_rather_than_being_placed():
    """The bound. A frame whose stream position is 899 s from where it
    actually arrived is not evidence about the timeline — it is evidence the
    anchor is stale."""
    rec = AudioRecorder()
    rec.caller_timestamp({"timestamp": "5000"}, 100.0)
    placed = rec.caller_timestamp({"timestamp": "5100"}, 999.0)
    assert placed == 999.0, "must fall back to arrival, not sit 899s away"
    assert rec._stream_reanchors == 1


def test_an_early_chunk_moves_the_origin_instead_of_being_dropped():
    """A chunk earlier than everything already fed is real audio, not an
    error. It used to be silently discarded: t0 was whichever chunk arrived
    first, so anything earlier computed a negative offset and hit the
    _build_timeline guard. Now the origin moves to accommodate it."""
    rec = AudioRecorder()
    rec.add_agent_chunk(10.0, _tone_frame(0))
    rec.add_caller_chunk(9.0, _tone_frame(1))     # 1 s BEFORE the agent chunk
    caller, agent = _decode(rec.to_wav_bytes())
    _assert_frame_at(caller, 1, 0)
    _assert_frame_at(agent, 0, 8000)


def test_the_negative_offset_guard_still_protects_the_buffer():
    """The guard is now unreachable through the feed API, so it is exercised
    directly. A negative slice index would write at the END of the buffer and
    silently corrupt the tail of a recording."""

    pcm = _build_timeline([(5.0, _tone_frame(0))], t0=10.0, total_samples=8000)
    assert not np.any(np.frombuffer(pcm, dtype=np.int16)), (
        "an out-of-range chunk must be dropped, never wrapped to the tail"
    )


# ── what the anchor can and cannot absorb ─────────────────────────────────────
# It pairs one frame's arrival with that same frame's stream position, so a
# delay in reaching us cancels. A difference in clock RATE does not — it
# accumulates — and a caller channel seconds out of turn is a worse recording
# than one with frame-scale roughness. Hence the bound.

_FAST_FRAMES = 400           # 8 s — long enough for 5% skew to exceed the bound


def _fast_stream_clock(rec: AudioRecorder) -> list[float]:
    """8 s of frames whose stream clock runs 5% fast against real arrivals."""
    return [
        rec.caller_timestamp({"timestamp": str(seq * 20 * 1.05)}, seq * 0.020)
        for seq in range(_FAST_FRAMES)
    ]


def test_a_stream_clock_that_runs_fast_cannot_walk_the_channel_away():
    """What the anchor cannot absorb is the two clocks advancing at different
    RATES — that accumulates. The bound catches it and the channel stays in
    step with the agent leg, which is placed on a monotonic sample clock."""
    rec = AudioRecorder()
    rec.set_stream_epoch(0.0)
    placed = _fast_stream_clock(rec)
    drift = max(abs(p - seq * 0.020) for seq, p in enumerate(placed))
    assert drift <= _MAX_DRIFT_SEC + 1e-9, f"channel drifted {drift:.3f}s from real time"
    assert rec._stream_reanchors > 0, "a fast stream clock must trigger re-anchoring"


def test_jitter_is_still_removed_inside_the_drift_window():
    """The bound must not undo the fix: frames whose arrival jitters but whose
    stream clock is honest are still placed on an even grid."""
    rec = AudioRecorder()
    rec.set_stream_epoch(0.0)
    rng = np.random.default_rng(3)
    placed = []
    for seq in range(100):
        arrival = seq * 0.020 + rng.uniform(-0.009, 0.017)   # measured jitter
        placed.append(rec.caller_timestamp({"timestamp": str(seq * 20)}, arrival))
    spacing = np.diff(placed)
    assert np.allclose(spacing, 0.020, atol=1e-6), (
        f"frames should stay exactly one period apart; spread="
        f"{spacing.min():.6f}..{spacing.max():.6f}"
    )
    assert rec._stream_reanchors == 0, "honest stream clock should never re-anchor"


# ── the timeline origin ───────────────────────────────────────────────────────
# The stream begins when the call connects — before the agent greets and before
# the borrower says anything. Taking t0 there rather than from the first chunk
# fed makes the origin independent of who speaks first.

def test_the_epoch_is_the_timeline_origin():
    rec = AudioRecorder()
    rec.set_stream_epoch(100.0)
    assert rec._t0 == 100.0, "the recording starts when the stream does"


def test_a_greeting_recorded_first_does_not_claim_the_origin():
    """The agent's first TTS frame can be fed before the first caller frame
    clears the STT gate. If that frame set t0, a caller frame from earlier in
    the call would compute a negative offset and be dropped by
    _build_timeline. Anchoring t0 on the stream epoch — which precedes both —
    removes the ordering dependency."""
    rec = AudioRecorder()
    rec.set_stream_epoch(100.0)
    rec.add_agent_chunk(100.5, _tone_frame(0))                        # greeting, fed first
    caller_ts = rec.caller_timestamp({"timestamp": "0"}, 100.2)       # but spoken earlier
    rec.add_caller_chunk(caller_ts, _tone_frame(1))

    assert rec._t0 == 100.0, "the origin is the call, not whoever was recorded first"
    caller, agent = _decode(rec.to_wav_bytes())
    assert np.any(caller), "the caller frame must survive, not be dropped before t0"
    _assert_frame_at(caller, 1, round(0.2 * 8000))
    _assert_frame_at(agent, 0, round(0.5 * 8000))


def test_without_an_epoch_the_origin_is_still_the_earliest_chunk():
    """Fallback path: no "start" event, so t0 comes from traffic — but it is
    the minimum, never merely the first one fed."""
    rec = AudioRecorder()
    rec.add_agent_chunk(100.5, _tone_frame(0))
    rec.add_caller_chunk(100.2, _tone_frame(1))
    assert rec._t0 == 100.2
    caller, agent = _decode(rec.to_wav_bytes())
    _assert_frame_at(caller, 1, 0)
    _assert_frame_at(agent, 0, round(0.3 * 8000))
