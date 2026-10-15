"""AmbienceMixer — pure DSP core for call-liveliness background sound.

No asyncio, no sockets, no clock. The mixer advances ONLY when ``next_frame``
is called, and every timing decision (when to duck, when to fire typing) is an
explicit method call made by the transport/playout layer. That keeps this
component fully deterministic and unit-testable — see tests/test_ambience_mixer.py.

Everything here is **linear16 mono** PCM. Mixing linear PCM is a straight
per-sample sum; μ-law must be decoded to linear before it reaches this class and
re-encoded after (μ-law is nonlinear — you cannot sum μ-law bytes). See
docs/ambience-liveliness.md.

Layering, per output frame:

    room tone (looped, ducked while speaking)
  + typing burst (one-shot, fast fade on stop / barge-in)
  + tts frame (only while speaking; supplied by the caller)
  ─────────────────────────────────────────────
  = clamped int16 frame

Gains ramp per-sample within the transition frames (linear slew) so a duck or a
typing-stop never produces a click at the frame boundary.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum

from vogon.gateway.transport.ambience.sources import Source

_SAMPLE_WIDTH = 2
_INT16_MAX = 32767
_INT16_MIN = -32768


def _clamp_i16(v: int) -> int:
    if v > _INT16_MAX:
        return _INT16_MAX
    if v < _INT16_MIN:
        return _INT16_MIN
    return v


class TypingState(Enum):
    IDLE = "idle"        # not playing, not armed
    PLAYING = "playing"  # a burst is sounding at full gain
    STOPPING = "stopping"  # fading out (fast) toward silence


@dataclass
class AmbienceConfig:
    """Tuning for one mixer instance. Loudness of the room bed is mostly baked
    into the source itself; ``room_gain`` is a final trim on top of it."""

    room_gain: float = 1.0          # trim on the (already-quiet) room source
    duck_factor: float = 0.5        # room gain multiplier while speaking (−6 dB)
    duck_ramp_ms: float = 60.0      # attack/release for the duck
    typing_gain: float = 0.15       # trim on the typing source — well below the bed/speech
    typing_fade_ms: float = 100.0   # fast fade when typing is stopped/barged
    oneshot_gain: float = 0.6       # trim on sparse one-shot events (kept low)


class AmbienceMixer:
    """Stateful linear16 mixer. One instance per call.

    Construct with a sample rate and optional room/typing sources (each a
    ``Source`` already resampled to that rate). ``next_frame`` produces the next
    output frame; ``set_speaking`` / ``start_typing`` / ``stop_typing`` change
    state for subsequent frames.
    """

    def __init__(
        self,
        sample_rate: int,
        room: Source | None = None,
        typing: Source | None = None,
        oneshots: Source | None = None,
        config: AmbienceConfig | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self._room = room
        self._typing = typing
        self._oneshots = oneshots
        self.cfg = config or AmbienceConfig()

        # Room bed: continuous loop cursor + ducking gain.
        self._room_cursor = 0
        base = self.cfg.room_gain
        self._amb_gain = base            # current (ramped) room gain
        self._amb_target = base          # target room gain
        self._amb_span = base * (1.0 - self.cfg.duck_factor) or base  # ramp distance

        # Typing: a one-shot burst with its own playhead + fade gain.
        self._typing_state = TypingState.IDLE
        self._typing_clip_idx = 0
        self._typing_pos = 0
        self._typing_gain = 0.0
        self._typing_target = 0.0

        # One-shots: sparse texture events (distant ring, chair creak). One at a
        # time, played to completion at a fixed low gain — no fade needed.
        self._oneshot_clip_idx = 0
        self._oneshot_pos = 0
        self._oneshot_active = False

    # ── State transitions (called by the transport/playout layer) ─────────────

    def set_speaking(self, speaking: bool) -> None:
        """Duck the room bed while TTS is speaking; unduck when it stops."""
        base = self.cfg.room_gain
        self._amb_target = base * self.cfg.duck_factor if speaking else base

    def start_typing(self, clip_index: int) -> None:
        """Begin a typing burst at full gain (the caller picks the clip index,
        rotating it per turn so successive turns don't replay the same clip)."""
        if self._typing is None:
            return
        self._typing_clip_idx = clip_index % len(self._typing)
        self._typing_pos = 0
        self._typing_gain = self.cfg.typing_gain
        self._typing_target = self.cfg.typing_gain
        self._typing_state = TypingState.PLAYING

    def stop_typing(self) -> None:
        """Fast-fade the typing burst to silence (first TTS chunk, or barge-in).
        Idempotent: a no-op when nothing is playing."""
        if self._typing_state == TypingState.PLAYING:
            self._typing_state = TypingState.STOPPING
            self._typing_target = 0.0

    def trigger_oneshot(self, clip_index: int) -> None:
        """Play a sparse one-shot event to completion. A no-op if one is already
        sounding (never overlap one-shots) or if there is no one-shot source.
        The caller gates this to between-turns only — never during speech."""
        if self._oneshots is None or self._oneshot_active:
            return
        self._oneshot_clip_idx = clip_index % len(self._oneshots)
        self._oneshot_pos = 0
        self._oneshot_active = True

    @property
    def oneshot_active(self) -> bool:
        return self._oneshot_active

    @property
    def typing_state(self) -> TypingState:
        return self._typing_state

    @property
    def room_gain_now(self) -> float:
        return self._amb_gain

    # ── Frame production ──────────────────────────────────────────────────────

    def next_frame(self, n_samples: int, tts_frame: bytes | None = None) -> bytes:
        """Produce the next mixed linear16 frame of ``n_samples`` samples.

        ``tts_frame`` — when speaking, the caller passes the TTS frame to mix the
        ambience *into*; it must be ``n_samples`` of linear16. When it is None
        (between turns / latency gap), an ambience-only frame is produced.

        Always returns exactly ``n_samples * 2`` bytes (silence is zeros), so the
        playout layer gets a continuous stream.
        """
        room = self._room_layer(n_samples)
        typing = self._typing_layer(n_samples)
        oneshot = self._oneshot_layer(n_samples)

        tts: list[int] | None = None
        if tts_frame is not None:
            # Tolerate a short final frame by truncating to the shorter length.
            count = min(n_samples, len(tts_frame) // _SAMPLE_WIDTH)
            tts = list(struct.unpack(f"<{count}h", tts_frame[: count * _SAMPLE_WIDTH]))
            if count < n_samples:
                tts.extend([0] * (n_samples - count))

        out = [0] * n_samples
        for i in range(n_samples):
            s = room[i] + typing[i] + oneshot[i]
            if tts is not None:
                s += tts[i]
            out[i] = _clamp_i16(s)
        return struct.pack(f"<{n_samples}h", *out)

    # ── Layers ────────────────────────────────────────────────────────────────

    def _ramp_gains(self, n_samples: int) -> tuple[float, float, float, float]:
        """Advance the room-duck and typing-fade gains by one frame; return
        (room_g0, room_g1, typing_g0, typing_g1) for per-sample interpolation."""
        dt = n_samples / self.sample_rate

        amb0 = self._amb_gain
        if self._amb_gain != self._amb_target:
            step = (dt / (self.cfg.duck_ramp_ms / 1000.0)) * self._amb_span
            if self._amb_gain < self._amb_target:
                self._amb_gain = min(self._amb_target, self._amb_gain + step)
            else:
                self._amb_gain = max(self._amb_target, self._amb_gain - step)
        amb1 = self._amb_gain

        typ0 = self._typing_gain
        if self._typing_state == TypingState.STOPPING and self._typing_gain > 0.0:
            step = (dt / (self.cfg.typing_fade_ms / 1000.0)) * self.cfg.typing_gain
            self._typing_gain = max(0.0, self._typing_gain - step)
            if self._typing_gain == 0.0:
                self._typing_state = TypingState.IDLE
        typ1 = self._typing_gain
        return amb0, amb1, typ0, typ1

    def _room_layer(self, n_samples: int) -> list[int]:
        amb0, amb1, _, _ = self._ramp_gains(n_samples)
        if self._room is None:
            return [0] * n_samples
        raw = self._read_loop(self._room.clip(0), n_samples)
        if amb0 == amb1:
            g = amb0
            return [int(raw[i] * g) for i in range(n_samples)]
        return [int(raw[i] * (amb0 + (amb1 - amb0) * (i / n_samples))) for i in range(n_samples)]

    def _typing_layer(self, n_samples: int) -> list[int]:
        # Gains were already advanced by _room_layer's _ramp_gains call this
        # frame; read the current typing gain values back for interpolation.
        typ_now = self._typing_gain
        if self._typing_state == TypingState.IDLE and typ_now == 0.0:
            return [0] * n_samples
        if self._typing is None:
            return [0] * n_samples

        clip = self._typing.clip(self._typing_clip_idx)
        raw = self._read_oneshot(clip, n_samples)
        g = typ_now  # constant across the frame is fine at 100 ms fade / 20 ms frames
        return [int(raw[i] * g) for i in range(n_samples)]

    def _oneshot_layer(self, n_samples: int) -> list[int]:
        if not self._oneshot_active or self._oneshots is None:
            return [0] * n_samples
        clip = self._oneshots.clip(self._oneshot_clip_idx)
        total = clip.n_samples
        pcm = clip.pcm
        g = self.cfg.oneshot_gain
        out: list[int] = []
        cur = self._oneshot_pos
        for _ in range(n_samples):
            if cur < total:
                out.append(int(struct.unpack("<h", pcm[cur * 2 : cur * 2 + 2])[0] * g))
                cur += 1
            else:
                out.append(0)
        self._oneshot_pos = cur
        if cur >= total:
            self._oneshot_active = False
        return out

    # ── Sample readers ────────────────────────────────────────────────────────

    def _read_loop(self, clip, n_samples: int) -> list[int]:
        """Read ``n_samples`` from a looping clip, wrapping seamlessly."""
        total = clip.n_samples
        if total == 0:
            return [0] * n_samples
        pcm = clip.pcm
        out: list[int] = []
        cur = self._room_cursor
        remaining = n_samples
        while remaining > 0:
            take = min(remaining, total - cur)
            out.extend(struct.unpack(f"<{take}h", pcm[cur * 2 : (cur + take) * 2]))
            cur += take
            if cur >= total:
                cur = 0
            remaining -= take
        self._room_cursor = cur
        return out

    def _read_oneshot(self, clip, n_samples: int) -> list[int]:
        """Read ``n_samples`` from a non-looping clip; zero-pad past the end and
        end the burst when the clip is exhausted."""
        total = clip.n_samples
        pcm = clip.pcm
        out: list[int] = []
        cur = self._typing_pos
        for _ in range(n_samples):
            if cur < total:
                out.append(struct.unpack("<h", pcm[cur * 2 : cur * 2 + 2])[0])
                cur += 1
            else:
                out.append(0)
        self._typing_pos = cur
        if cur >= total and self._typing_state == TypingState.PLAYING:
            # Burst played out naturally.
            self._typing_state = TypingState.IDLE
            self._typing_gain = 0.0
        return out
