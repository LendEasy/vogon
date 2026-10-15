"""Ambience audio sources — decoded, pre-resampled linear16 mono PCM.

A *source* is a bank of one or more mono linear16 loops/clips at a single
sample rate, ready to be read frame-by-frame by the mixer. Sources never
resample on the hot path: they are decoded and resampled to the target rate
ONCE at construction.

Two providers:

- ``WavSource`` — loads real recordings from ``assets/``.
- ``SyntheticSource`` — a deterministic, seeded generator (filtered noise for
  room tone, click trains for typing). No ``random`` module, no wall clock, so
  the same seed yields byte-identical audio every run — the mixer's tests, and
  a usable-but-basic default before real recordings are dropped in, both rely
  on that determinism.

All audio here is **linear16 mono** (2 bytes/sample, signed, little-endian).
Mixing MUST happen in linear PCM — never μ-law — so μ-law encoding is left to
the transport boundary, not this module.
"""
from __future__ import annotations

import audioop
import logging
import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SAMPLE_WIDTH = 2  # linear16
_INT16_MAX = 32767
_INT16_MIN = -32768

# Native rates we pre-resample every clip to, so the hot path never resamples.
RATE_WIDGET = 24000   # VoiceTransport linear16
RATE_PHONE = 8000     # Twilio, after μ-law decode


def _clamp_i16(v: int) -> int:
    if v > _INT16_MAX:
        return _INT16_MAX
    if v < _INT16_MIN:
        return _INT16_MIN
    return v


class _Lcg:
    """Deterministic linear-congruential generator (Numerical Recipes params).

    Used instead of ``random``/``Math.random`` so synthetic audio is byte-
    identical across runs given a seed — a hard requirement for reproducible
    mixer tests and for resume-safety consistency in the wider codebase.
    """

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        self._state = seed & 0xFFFFFFFF

    def next_u32(self) -> int:
        self._state = (1664525 * self._state + 1013904223) & 0xFFFFFFFF
        return self._state

    def next_unit(self) -> float:
        """Uniform float in [-1.0, 1.0)."""
        return (self.next_u32() / 0xFFFFFFFF) * 2.0 - 1.0


@dataclass
class Clip:
    """A single mono linear16 clip at one sample rate."""

    pcm: bytes           # linear16 mono
    sample_rate: int

    @property
    def n_samples(self) -> int:
        return len(self.pcm) // _SAMPLE_WIDTH


class Source:
    """A bank of clips at one sample rate, addressable by index."""

    def __init__(self, clips: list[Clip], sample_rate: int) -> None:
        if not clips:
            raise ValueError("Source requires at least one clip")
        if any(c.sample_rate != sample_rate for c in clips):
            raise ValueError("all clips must share the source sample rate")
        self.clips = clips
        self.sample_rate = sample_rate

    def __len__(self) -> int:
        return len(self.clips)

    def clip(self, index: int) -> Clip:
        return self.clips[index % len(self.clips)]


# ── WAV-backed source ─────────────────────────────────────────────────────────


def _load_wav_mono16(path: Path) -> tuple[bytes, int]:
    """Load a WAV as mono linear16 PCM, returning (pcm, sample_rate).

    Downmixes stereo to mono and widens/narrows sample width to 16-bit; does
    NOT resample (that is done once, per target rate, in build_source()).
    """
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    if width != _SAMPLE_WIDTH:
        pcm = audioop.lin2lin(pcm, width, _SAMPLE_WIDTH)
    if n_channels == 2:
        pcm = audioop.tomono(pcm, _SAMPLE_WIDTH, 0.5, 0.5)
    elif n_channels != 1:
        raise ValueError(f"{path.name}: unsupported channel count {n_channels}")
    return pcm, rate


def _resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return pcm
    converted, _ = audioop.ratecv(pcm, _SAMPLE_WIDTH, 1, src_rate, dst_rate, None)
    return converted


def build_wav_source(paths: list[Path], target_rate: int) -> Source:
    """Decode WAV files to mono linear16 at ``target_rate`` (resampled once)."""
    clips: list[Clip] = []
    for p in paths:
        pcm, rate = _load_wav_mono16(p)
        clips.append(Clip(pcm=_resample(pcm, rate, target_rate), sample_rate=target_rate))
    return Source(clips, target_rate)


# ── Synthetic source (deterministic fallback + test fixture) ──────────────────


def _one_pole_lowpass(pcm_samples: list[int], alpha: float) -> list[int]:
    """Simple one-pole low-pass to take the harsh edge off white noise, so it
    reads as diffuse room tone rather than hiss. alpha in (0,1]; lower = duller."""
    out: list[int] = []
    prev = 0.0
    for s in pcm_samples:
        prev = prev + alpha * (s - prev)
        out.append(_clamp_i16(int(prev)))
    return out


def _synth_room_tone(sample_rate: int, seconds: float, seed: int, amp: float) -> bytes:
    """Low-passed pseudo-noise bed — a stand-in office-floor room tone.

    ``amp`` is the peak fraction of full scale (e.g. 0.03 ≈ −30 dBFS-ish before
    the mixer's own gain). Deterministic for a given (rate, seconds, seed).
    """
    n = int(sample_rate * seconds)
    rng = _Lcg(seed)
    peak = amp * _INT16_MAX
    raw = [int(rng.next_unit() * peak) for _ in range(n)]
    shaped = _one_pole_lowpass(raw, alpha=0.06)
    # Force loop seamlessness: crossfade the tail into the head over ~30 ms so
    # the wrap point has no discontinuity (clicks at the seam are the classic
    # ambient-loop tell).
    xf = min(int(sample_rate * 0.03), n // 4)
    if xf > 0:
        for i in range(xf):
            w = i / xf
            head = shaped[i]
            tail = shaped[n - xf + i]
            blended = int(tail * (1.0 - w) + head * w)
            shaped[n - xf + i] = _clamp_i16(blended)
    return struct.pack(f"<{n}h", *shaped)


def _synth_typing_burst(sample_rate: int, seed: int, amp: float) -> bytes:
    """A short keyboard-ish click train (~0.9–1.4 s), deterministic per seed.

    Each 'keystroke' is a fast-decaying filtered impulse; inter-key gaps vary so
    it doesn't sound metronomic. This is a stand-in until real recordings land —
    good enough to read as 'typing' under room tone, not a hi-fi asset.
    """
    rng = _Lcg(seed)
    n_keys = 7 + (rng.next_u32() % 6)          # 7..12 keystrokes
    peak = amp * _INT16_MAX
    samples: list[int] = []
    for _ in range(n_keys):
        # gap before this key: 60..160 ms
        gap = int(sample_rate * (0.06 + 0.10 * ((rng.next_u32() % 1000) / 1000.0)))
        samples.extend([0] * gap)
        # click: fast attack, exponential decay over ~25..45 ms
        dur = int(sample_rate * (0.025 + 0.020 * ((rng.next_u32() % 1000) / 1000.0)))
        for i in range(dur):
            env = math.exp(-i / (dur * 0.35))
            samples.append(_clamp_i16(int(rng.next_unit() * peak * env)))
    return struct.pack(f"<{len(samples)}h", *samples)


def build_synthetic_room_tone(target_rate: int, seed: int = 0x51EED) -> Source:
    return Source([Clip(_synth_room_tone(target_rate, 4.0, seed, amp=0.06), target_rate)], target_rate)


def build_synthetic_typing(target_rate: int, seed_base: int = 0x7A11, n: int = 4) -> Source:
    clips = [
        Clip(_synth_typing_burst(target_rate, seed_base + i, amp=0.5), target_rate)
        for i in range(n)
    ]
    return Source(clips, target_rate)


def _synth_distant_ring(sample_rate: int, seed: int, amp: float) -> bytes:
    """A faint, far-off phone ring: two short warble bursts, low-passed and
    quiet, as if from across an office. Deterministic per seed."""
    rng = _Lcg(seed)
    sr = sample_rate
    burst = int(sr * 0.35)
    gap = int(sr * 0.20)
    peak = amp * _INT16_MAX
    samples: list[int] = []
    for _b in range(2):
        for i in range(burst):
            # 480 Hz carrier warbled at ~20 Hz, gentle attack/decay envelope.
            t = i / sr
            carrier = math.sin(2 * math.pi * 480 * t)
            warble = 0.5 + 0.5 * math.sin(2 * math.pi * 20 * t)
            env = math.sin(math.pi * i / burst)  # smooth in/out
            samples.append(_clamp_i16(int(carrier * warble * env * peak)))
        samples.extend([0] * gap)
    shaped = _one_pole_lowpass(samples, alpha=0.25)
    # A hair of dither so it doesn't sound like a pure test tone.
    for i in range(len(shaped)):
        shaped[i] = _clamp_i16(shaped[i] + int(rng.next_unit() * peak * 0.02))
    return struct.pack(f"<{len(shaped)}h", *shaped)


def _synth_chair_creak(sample_rate: int, seed: int, amp: float) -> bytes:
    """A short low-frequency creak — a filtered noise swell, ~0.4 s."""
    rng = _Lcg(seed)
    n = int(sample_rate * 0.4)
    peak = amp * _INT16_MAX
    raw = [int(rng.next_unit() * peak) for _ in range(n)]
    shaped = _one_pole_lowpass(raw, alpha=0.02)  # very dull → low rumble
    for i in range(n):
        env = math.sin(math.pi * i / n)
        shaped[i] = _clamp_i16(int(shaped[i] * env))
    return struct.pack(f"<{n}h", *shaped)


def build_synthetic_oneshots(target_rate: int, seed_base: int = 0x0EE) -> Source:
    """A small bank of sparse, quiet one-shot events (distant ring, chair creak).
    Kept intentionally low-amplitude — these are texture, not focal sounds."""
    clips = [
        Clip(_synth_distant_ring(target_rate, seed_base, amp=0.18), target_rate),
        Clip(_synth_chair_creak(target_rate, seed_base + 1, amp=0.30), target_rate),
    ]
    return Source(clips, target_rate)
