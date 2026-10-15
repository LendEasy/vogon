"""Frame-math tests for the pure AmbienceMixer (Phase 1).

No audio device, no asyncio, no network — the mixer advances only when
next_frame() is called, so every property here is deterministic. These pin the
DSP contract the transport layer will depend on in Phases 2–3.
"""
from __future__ import annotations

import audioop
import struct

import pytest

from vogon.gateway.transport.ambience import sources as src
from vogon.gateway.transport.ambience.loader import build_mixer
from vogon.gateway.transport.ambience.mixer import (
    AmbienceConfig,
    AmbienceMixer,
    TypingState,
)

RATE = 8000
FRAME = 160  # 20 ms at 8 kHz — the Twilio frame size


def _unpack(frame: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(frame) // 2}h", frame))


def _peak(frame: bytes) -> int:
    return max((abs(s) for s in _unpack(frame)), default=0)


def _const_source(value: int, n: int = FRAME * 4) -> src.Source:
    pcm = struct.pack(f"<{n}h", *([value] * n))
    return src.Source([src.Clip(pcm, RATE)], RATE)


# ── synthetic source determinism ──────────────────────────────────────────────

def test_synthetic_sources_are_deterministic():
    a = src.build_synthetic_typing(RATE, seed_base=0x7A11, n=4)
    b = src.build_synthetic_typing(RATE, seed_base=0x7A11, n=4)
    assert [c.pcm for c in a.clips] == [c.pcm for c in b.clips]

    r1 = src.build_synthetic_room_tone(RATE, seed=0x51EED)
    r2 = src.build_synthetic_room_tone(RATE, seed=0x51EED)
    assert r1.clips[0].pcm == r2.clips[0].pcm


def test_synthetic_room_tone_is_audible_but_quiet():
    room = src.build_synthetic_room_tone(RATE)
    peak = max(abs(s) for s in _unpack(room.clips[0].pcm))
    assert peak > 0, "room tone must not be pure silence"
    assert peak < 0.15 * 32767, "room tone must sit low, near the noise floor"


# ── frame shape + silence-in → ambience-out ───────────────────────────────────

def test_next_frame_is_always_full_length():
    m = build_mixer(RATE)
    for _ in range(10):
        assert len(m.next_frame(FRAME)) == FRAME * 2


def test_between_turns_frame_carries_room_tone_not_silence():
    m = build_mixer(RATE, enable_typing=False)
    # Sum energy over a second so we don't sample a momentary zero-crossing.
    energy = sum(_peak(m.next_frame(FRAME)) for _ in range(50))
    assert energy > 0, "the line must never be digitally silent between turns"


def test_disabled_mixer_emits_pure_silence():
    m = AmbienceMixer(RATE, room=None, typing=None)
    assert _peak(m.next_frame(FRAME)) == 0


# ── loop wrap ─────────────────────────────────────────────────────────────────

def test_room_loop_wraps_seamlessly_and_periodically():
    room = _const_source(1000)  # constant clip → any read is 1000
    total = room.clips[0].n_samples
    m = AmbienceMixer(RATE, room=room, typing=None, config=AmbienceConfig(room_gain=1.0))

    # Read two full periods frame-by-frame; the cursor must wrap so that
    # period 2 reproduces period 1 exactly (no dropped/duplicated samples).
    n_frames = (total // FRAME) * 2
    frames = [m.next_frame(FRAME) for _ in range(n_frames)]
    half = n_frames // 2
    assert frames[:half] == frames[half:], "loop is not exactly periodic — cursor wrap is wrong"
    # Constant source → every sample is the constant, proving the wrap seam
    # introduced no gap.
    assert all(s == 1000 for s in _unpack(frames[0]))


# ── duck ramp ─────────────────────────────────────────────────────────────────

def test_duck_ramps_down_gradually_and_settles():
    cfg = AmbienceConfig(room_gain=1.0, duck_factor=0.5, duck_ramp_ms=60.0)
    m = AmbienceMixer(RATE, room=_const_source(10000), typing=None, config=cfg)

    m.next_frame(FRAME)                 # establish baseline gain (1.0)
    assert m.room_gain_now == pytest.approx(1.0)

    m.set_speaking(True)
    gains = []
    for _ in range(6):
        m.next_frame(FRAME)
        gains.append(m.room_gain_now)

    # Monotonic non-increasing, gradual (no instant jump to target), settles at 0.5.
    assert gains[0] < 1.0 and gains[0] > 0.5, "duck must ramp, not jump"
    assert all(b <= a + 1e-9 for a, b in zip(gains, gains[1:], strict=False)), "duck must be monotonic down"
    assert gains[-1] == pytest.approx(0.5), "duck must settle at duck_factor"


def test_unduck_ramps_back_up():
    cfg = AmbienceConfig(room_gain=1.0, duck_factor=0.5, duck_ramp_ms=60.0)
    m = AmbienceMixer(RATE, room=_const_source(10000), typing=None, config=cfg)
    m.set_speaking(True)
    for _ in range(6):
        m.next_frame(FRAME)
    assert m.room_gain_now == pytest.approx(0.5)

    m.set_speaking(False)
    for _ in range(6):
        m.next_frame(FRAME)
    assert m.room_gain_now == pytest.approx(1.0)


# ── typing lifecycle ──────────────────────────────────────────────────────────

def test_typing_starts_plays_and_stops_on_command():
    typing = _const_source(8000)  # constant so energy is unambiguous
    m = AmbienceMixer(RATE, room=None, typing=typing,
                      config=AmbienceConfig(typing_gain=1.0, typing_fade_ms=100.0))
    assert m.typing_state == TypingState.IDLE
    assert _peak(m.next_frame(FRAME)) == 0

    m.start_typing(0)
    assert m.typing_state == TypingState.PLAYING
    assert _peak(m.next_frame(FRAME)) > 0, "typing must be audible while playing"

    m.stop_typing()
    assert m.typing_state == TypingState.STOPPING
    # Within the fade window it decays to silence and returns to IDLE.
    for _ in range(10):
        m.next_frame(FRAME)
    assert m.typing_state == TypingState.IDLE
    assert _peak(m.next_frame(FRAME)) == 0


def test_typing_stop_is_a_fade_not_a_cliff():
    typing = _const_source(8000)
    m = AmbienceMixer(RATE, room=None, typing=typing,
                      config=AmbienceConfig(typing_gain=1.0, typing_fade_ms=100.0))
    m.start_typing(0)
    m.next_frame(FRAME)
    m.stop_typing()
    p1 = _peak(m.next_frame(FRAME))  # first fade frame — still audible, quieter
    assert 0 < p1 < 8000, "stop must fade, not cut to silence in one frame"


def test_typing_ends_naturally_when_clip_exhausts():
    short = src.Source([src.Clip(struct.pack(f"<{200}h", *([5000] * 200)), RATE)], RATE)
    m = AmbienceMixer(RATE, room=None, typing=short)
    m.start_typing(0)
    # 200 samples < 2 frames; after enough frames the burst self-terminates.
    for _ in range(4):
        m.next_frame(FRAME)
    assert m.typing_state == TypingState.IDLE


def test_barge_in_stops_typing_fast():
    # A barge-in calls stop_typing() the same as first-chunk; the fast fade means
    # typing is silent within ~typing_fade_ms of the interrupt.
    typing = _const_source(8000)
    m = AmbienceMixer(RATE, room=None, typing=typing,
                      config=AmbienceConfig(typing_fade_ms=100.0))
    m.start_typing(0)
    m.next_frame(FRAME)
    m.stop_typing()
    # 100 ms fade / 20 ms frames = 5 frames to silence.
    for _ in range(6):
        m.next_frame(FRAME)
    assert m.typing_state == TypingState.IDLE
    assert _peak(m.next_frame(FRAME)) == 0


# ── mixing + clamp ────────────────────────────────────────────────────────────

def test_tts_frame_is_mixed_in_and_output_clamps_on_overflow():
    # room + tts both large positive → sum overflows int16; must clamp to +max,
    # never wrap to negative.
    m = AmbienceMixer(RATE, room=_const_source(20000), typing=None,
                      config=AmbienceConfig(room_gain=1.0, duck_factor=1.0))
    tts = struct.pack(f"<{FRAME}h", *([20000] * FRAME))
    out = _unpack(m.next_frame(FRAME, tts_frame=tts))
    assert all(s == 32767 for s in out), "overflow must clamp to int16 max, not wrap"


def test_tts_frame_mix_is_additive_when_in_range():
    m = AmbienceMixer(RATE, room=_const_source(1000), typing=None,
                      config=AmbienceConfig(room_gain=1.0, duck_factor=1.0))
    tts = struct.pack(f"<{FRAME}h", *([5000] * FRAME))
    out = _unpack(m.next_frame(FRAME, tts_frame=tts))
    assert all(s == 6000 for s in out), "in-range mix must be a plain sum (1000+5000)"


def test_oneshot_plays_to_completion_then_ends():
    shots = src.Source([src.Clip(struct.pack(f"<{300}h", *([9000] * 300)), RATE)], RATE)
    m = AmbienceMixer(RATE, room=None, typing=None, oneshots=shots,
                      config=AmbienceConfig(oneshot_gain=1.0))
    assert not m.oneshot_active
    assert _peak(m.next_frame(FRAME)) == 0

    m.trigger_oneshot(0)
    assert m.oneshot_active
    assert _peak(m.next_frame(FRAME)) == 9000  # 300 samples > 1 frame → audible

    for _ in range(3):
        m.next_frame(FRAME)
    assert not m.oneshot_active, "one-shot must self-terminate at clip end"
    assert _peak(m.next_frame(FRAME)) == 0


def test_oneshot_does_not_overlap_itself():
    shots = src.Source([src.Clip(struct.pack(f"<{2000}h", *([9000] * 2000)), RATE)], RATE)
    m = AmbienceMixer(RATE, room=None, typing=None, oneshots=shots)
    m.trigger_oneshot(0)
    m.next_frame(FRAME)
    pos_before = m._oneshot_pos
    m.trigger_oneshot(1)  # must be ignored while one is sounding
    m.next_frame(FRAME)
    assert m._oneshot_pos > pos_before, "the in-flight one-shot kept advancing"
    assert m._oneshot_clip_idx == 0, "a second trigger must not switch clips mid-play"


def test_oneshot_absent_source_is_safe():
    m = AmbienceMixer(RATE, room=None, typing=None, oneshots=None)
    m.trigger_oneshot(0)  # no-op, no crash
    assert not m.oneshot_active
    assert _peak(m.next_frame(FRAME)) == 0


def test_short_tts_frame_is_tolerated():
    m = AmbienceMixer(RATE, room=None, typing=None)
    short = struct.pack(f"<{100}h", *([1000] * 100))  # fewer than FRAME samples
    out = _unpack(m.next_frame(FRAME, tts_frame=short))
    assert len(out) == FRAME
    assert out[:100] == [1000] * 100 and out[100:] == [0] * (FRAME - 100)


# ── μ-law boundary (Phase 3 / Twilio) ─────────────────────────────────────────

def test_mulaw_mix_boundary_roundtrips_to_a_valid_frame():
    """The Twilio path is μ-law 8 kHz. Mixing must decode→mix(linear)→re-encode
    and produce a well-formed 160-byte μ-law frame the caller can play. This is
    the exact per-frame operation TwilioTransport._do_tts_stream performs."""

    m = build_mixer(RATE)  # 8 kHz synthetic room+typing
    m.set_speaking(True)   # duck under speech, as during real TTS
    # A μ-law "TTS" frame (160 bytes = 160 samples = 20 ms at 8 kHz).
    tts_linear = struct.pack(f"<{FRAME}h", *([6000] * FRAME))
    tts_mulaw = audioop.lin2ulaw(tts_linear, 2)
    assert len(tts_mulaw) == FRAME

    lin = audioop.ulaw2lin(tts_mulaw, 2)
    mixed = m.next_frame(FRAME, tts_frame=lin)
    out_mulaw = audioop.lin2ulaw(mixed, 2)

    assert len(out_mulaw) == FRAME, "μ-law frame must stay 160 bytes"
    # Decodes cleanly and carries the speech (room bed is quiet + ducked, so the
    # ~6000 speech level dominates — the mix didn't zero or wrap it).
    decoded = _unpack(audioop.ulaw2lin(out_mulaw, 2))
    assert max(decoded) > 3000, "speech must survive the μ-law mix round-trip"


def test_ambience_only_mulaw_frame_is_nonsilent():
    """Between-turn frames (no TTS) still carry the room bed once encoded to
    μ-law — the phone line is never digitally silent."""

    m = build_mixer(RATE, enable_typing=False)
    energy = 0
    for _ in range(25):
        mulaw = audioop.lin2ulaw(m.next_frame(FRAME), 2)
        energy += _peak(audioop.ulaw2lin(mulaw, 2))
    assert energy > 0
