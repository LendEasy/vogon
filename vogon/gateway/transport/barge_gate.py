"""BargeGate — pure barge-in decision core (Tier 2, docs/barge-in-tuning.md).

One object per call. The transport feeds it one cleaned frame at a time
(``on_frame``); it returns a ``FrameVerdict`` describing what the transport
should DO — cancel TTS, forward frames to STT, count noise — plus the audio
events to log. The gate itself performs **no I/O, no clock reads, no imports
of audio libraries**: time arrives as an argument, the VAD arrives as an
injected ``confirm_frame(pcm) -> bool`` callable. That is what makes every
transition unit-testable with stubs (tests/test_barge_gate.py).

Three layers, each independently switchable:

1. **Energy pre-gate (Tier 1)** — threshold + consecutive-frame debounce +
   start-grace. Always on; with everything else off the gate reproduces the
   Tier-1 inline behaviour exactly.
2. **Adaptive threshold (Phase A)** — the threshold tracks the line's own
   noise floor: an EMA over *noise-classified frames only* (speech/barge
   frames never update it, or the floor inflates and desensitises the gate),
   ``threshold = clamp(margin × floor, min, max)``, fixed threshold until the
   estimate warms up.
3. **Speech confirm (Phase B1)** — instead of cancelling at pre-gate maturity,
   open a PROVISIONAL window: TTS keeps playing, frames are buffered and fed
   to the injected VAD; M-of-N speech-positive frames confirm (cancel + flush
   the buffered onset to STT so nothing the caller said is lost), a timeout or
   K sub-threshold frames abort (nothing audible ever happened).

State machine (Phase B1; with confirm="off" PROVISIONAL is skipped —
pre-gate maturity IS confirmation):

    IDLE ──streak≥N & !grace──▶ PROVISIONAL ──M-of-N VAD──▶ CONFIRMED
                                    │  timeout / K quiet frames
                                    ▼
                                  ABORT → IDLE (streak reset, buffer dropped)

CONFIRMED latches until ``reset()`` (the transport calls it when leaving the
echo-gated window); while latched every frame forwards to STT so Deepgram
captures the caller's full turn.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class GateState(Enum):
    IDLE = "idle"
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"


@dataclass
class BargeGateConfig:
    # Tier 1 — energy pre-gate
    rms_threshold: int = 1200
    consecutive_frames: int = 3
    grace_sec: float = 0.100
    # Phase A — adaptive threshold
    adaptive_enabled: bool = False
    adaptive_alpha: float = 0.05
    adaptive_margin: float = 7.0
    adaptive_min: int = 600
    adaptive_max: int = 2600
    adaptive_warmup_frames: int = 50
    # Phase B1 — speech confirm
    confirm: str = "off"                 # "off" | "local_vad"
    confirm_frames_m: int = 3
    confirm_frames_n: int = 5
    confirm_timeout_sec: float = 0.400
    confirm_energy_floor_frames: int = 3


@dataclass
class FrameVerdict:
    """What the transport should do with this frame. The gate decides; the
    transport performs (cancel TTS / send clear, STT forwarding, logging)."""
    confirmed_now: bool = False          # cancel TTS / clear the buffer NOW
    forward_frames: list = field(default_factory=list)  # frames to send to STT (incl. flushed onset)
    noise_frame: bool = False            # count toward the SNR noise accumulators
    events: list = field(default_factory=list)          # (event_name, detail) for the audio log


class BargeGate:
    """Pure per-call barge decision core. See module docstring."""

    def __init__(
        self,
        cfg: BargeGateConfig,
        confirm_frame: Callable[[bytes], bool] | None = None,
    ) -> None:
        self.cfg = cfg
        self._confirm_frame = confirm_frame
        if cfg.confirm == "local_vad" and confirm_frame is None:
            # No VAD available (import failed): degrade to Tier-1 semantics
            # rather than silently never confirming a barge.
            self.cfg.confirm = "off"

        self.state = GateState.IDLE
        self.streak = 0
        self._tts_started_at: float = 0.0
        # Adaptive floor
        self._floor: float = 0.0
        self._floor_frames: int = 0
        # Provisional window
        self._prov_opened_at: float = 0.0
        self._prov_buffer: list[bytes] = []
        self._prov_vad_flags: list[int] = []
        self._prov_quiet_run: int = 0

    # ── Transport signals ─────────────────────────────────────────────────────

    def on_tts_start(self, now: float) -> None:
        """New agent turn: start the grace window, drop any stale streak, and
        abandon a provisional window opened against the previous turn."""
        self._tts_started_at = now
        self.streak = 0
        if self.state == GateState.PROVISIONAL:
            self._drop_provisional()
            self.state = GateState.IDLE
        # CONFIRMED is not reset here — reset() owns that (echo-window exit).

    def reset(self) -> None:
        """Leaving the echo-gated window: back to a clean IDLE."""
        self.state = GateState.IDLE
        self.streak = 0
        self._drop_provisional()

    @property
    def in_progress(self) -> bool:
        return self.state == GateState.CONFIRMED

    @property
    def threshold(self) -> int:
        """The active threshold: adaptive when enabled and warmed up."""
        cfg = self.cfg
        if not cfg.adaptive_enabled or self._floor_frames < cfg.adaptive_warmup_frames:
            return cfg.rms_threshold
        return int(
            min(cfg.adaptive_max, max(cfg.adaptive_min, cfg.adaptive_margin * self._floor))
        )

    @property
    def noise_floor(self) -> float:
        return self._floor

    # ── Per-frame decision ────────────────────────────────────────────────────

    def on_frame(
        self, now: float, clean_rms: int, tts_playing: bool, frame: bytes
    ) -> FrameVerdict:
        v = FrameVerdict()
        thr = self.threshold
        over = clean_rms > thr

        # CONFIRMED: everything forwards (Deepgram needs the full turn,
        # including mid-utterance quiet gaps).
        if self.state == GateState.CONFIRMED:
            v.forward_frames.append(frame)
            return v

        # Adaptive floor learns from noise frames only — and never inside a
        # provisional window (those frames are barge candidates, not ambience).
        if (
            self.cfg.adaptive_enabled
            and not over
            and self.state == GateState.IDLE
        ):
            a = self.cfg.adaptive_alpha
            self._floor = clean_rms if self._floor_frames == 0 else (
                a * clean_rms + (1.0 - a) * self._floor
            )
            self._floor_frames += 1

        # ── Streak bookkeeping (pre-gate) ─────────────────────────────────────
        if over:
            self.streak += 1
            if self.streak == 1 and self.state == GateState.IDLE:
                v.events.append(("barge_streak_start", {"clean_rms": clean_rms, "threshold": thr}))
        else:
            if self.streak > 0 and self.state == GateState.IDLE:
                v.events.append(("barge_suppressed", {"streak": self.streak, "clean_rms": clean_rms}))
            self.streak = 0

        in_grace = tts_playing and (now - self._tts_started_at) < self.cfg.grace_sec

        # ── PROVISIONAL: feed the confirm window ──────────────────────────────
        if self.state == GateState.PROVISIONAL:
            self._prov_buffer.append(frame)
            flag = 1 if (self._confirm_frame is not None and self._confirm_frame(frame)) else 0
            self._prov_vad_flags.append(flag)
            self._prov_quiet_run = 0 if over else self._prov_quiet_run + 1

            recent = self._prov_vad_flags[-self.cfg.confirm_frames_n:]
            if sum(recent) >= self.cfg.confirm_frames_m:
                latency = now - self._prov_opened_at
                v.confirmed_now = True
                v.forward_frames.extend(self._prov_buffer)  # onset not lost
                v.events.append(("barge_confirmed", {
                    "clean_rms": clean_rms,
                    "confirm": "local_vad",
                    "confirm_latency_ms": round(latency * 1000),
                    "vad_positive": sum(recent),
                    "window": len(self._prov_vad_flags),
                }))
                self.state = GateState.CONFIRMED
                self._drop_provisional()
                return v

            timed_out = (now - self._prov_opened_at) >= self.cfg.confirm_timeout_sec
            energy_dead = self._prov_quiet_run >= self.cfg.confirm_energy_floor_frames
            if timed_out or energy_dead:
                v.events.append(("barge_abort", {
                    "reason": "timeout" if timed_out else "energy_floor",
                    "window": len(self._prov_vad_flags),
                    "vad_positive": sum(self._prov_vad_flags),
                }))
                self.state = GateState.IDLE
                self.streak = 0
                self._drop_provisional()
                v.noise_frame = not over
                return v

            return v  # still deciding — nothing audible happens

        # ── IDLE: does the pre-gate fire? ─────────────────────────────────────
        mature = self.streak >= self.cfg.consecutive_frames
        if mature and in_grace:
            if self.streak == self.cfg.consecutive_frames:  # emit once per streak
                v.events.append(("barge_grace_blocked", {"streak": self.streak, "clean_rms": clean_rms}))
            return v

        if mature:
            if self.cfg.confirm == "off":
                # Tier-1 semantics: pre-gate maturity IS confirmation.
                v.confirmed_now = True
                v.forward_frames.append(frame)
                v.events.append(("barge_confirmed", {
                    "clean_rms": clean_rms, "confirm": "off", "streak": self.streak,
                }))
                self.state = GateState.CONFIRMED
            else:
                self.state = GateState.PROVISIONAL
                self._prov_opened_at = now
                self._prov_buffer = [frame]
                flag = 1 if (self._confirm_frame is not None and self._confirm_frame(frame)) else 0
                self._prov_vad_flags = [flag]
                self._prov_quiet_run = 0
                v.events.append(("barge_provisional_open", {
                    "clean_rms": clean_rms, "streak": self.streak, "threshold": thr,
                }))
            return v

        # Below threshold, nothing in flight: an ambience/noise frame.
        if not over:
            v.noise_frame = True
        return v

    # ── Internals ─────────────────────────────────────────────────────────────

    def _drop_provisional(self) -> None:
        self._prov_buffer = []
        self._prov_vad_flags = []
        self._prov_quiet_run = 0
        self._prov_opened_at = 0.0


# ── Confirm backend factory ────────────────────────────────────────────────────

def make_confirm_frame(confirm: str, vad_aggressiveness: int, sample_rate: int = 8000):
    """Build the injected per-frame speech predicate, or None.

    local_vad → webrtcvad at the configured mode over linear16 frames.
    Returns None when the backend is "off" or webrtcvad is unavailable (the
    gate then degrades to Tier-1 semantics).
    """
    if confirm != "local_vad":
        return None
    try:
        import webrtcvad  # type: ignore[import]  # noqa: PLC0415 — optional dependency (ImportError -> None)
    except ImportError:
        return None
    vad = webrtcvad.Vad(vad_aggressiveness)

    def _is_speech(pcm: bytes) -> bool:
        try:
            return vad.is_speech(pcm, sample_rate)
        except Exception:
            return False

    return _is_speech
