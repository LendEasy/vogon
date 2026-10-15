"""BargeGate FSM + adaptive threshold — pure unit tests (no audio, no VAD lib).

Pins the full transition table of docs/barge-in-tuning.md's Tier-2 design:
Tier-1 parity with confirm=off, the adaptive noise floor (Phase A), and the
PROVISIONAL → CONFIRMED / ABORT machine with a stubbed confirm (Phase B1).
Time is passed in; the VAD is a lambda — everything is deterministic.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import struct

from vogon.gateway.settings import settings
from vogon.gateway.transport.barge_gate import BargeGate, BargeGateConfig, GateState
from vogon.gateway.transport.twilio_transport import TwilioTransport

FRAME = b"\x00" * 320  # contents irrelevant to the gate (RMS arrives separately)


def _cfg(**kw) -> BargeGateConfig:
    return BargeGateConfig(**kw)


def _run(gate, seq, t0=10.0, dt=0.02, tts=True):
    """Feed (rms) frames at 20 ms cadence; return list of verdicts."""
    out = []
    for i, rms in enumerate(seq):
        out.append(gate.on_frame(t0 + i * dt, rms, tts, FRAME))
    return out


def _events(verdicts):
    return [e for v in verdicts for e, _ in v.events]


# ── Tier-1 parity (confirm=off) ────────────────────────────────────────────────

def test_off_single_loud_frame_does_not_confirm():
    g = BargeGate(_cfg())
    g.on_tts_start(0.0)
    vs = _run(g, [5000, 200, 200])
    assert not any(v.confirmed_now for v in vs)
    assert "barge_suppressed" in _events(vs)


def test_off_debounce_confirms_at_n_and_latches():
    g = BargeGate(_cfg())
    g.on_tts_start(0.0)
    vs = _run(g, [5000, 5000, 5000, 100])
    assert vs[2].confirmed_now and g.state == GateState.CONFIRMED
    assert vs[2].forward_frames, "the confirming frame must reach STT"
    # Latched: everything after forwards, even quiet gaps.
    assert vs[3].forward_frames and not vs[3].confirmed_now


def test_off_grace_blocks_then_allows():
    g = BargeGate(_cfg())
    g.on_tts_start(10.0)  # grace 100 ms → frames at 10.00/10.02/10.04 in grace
    vs = _run(g, [5000, 5000, 5000], t0=10.0)
    assert not any(v.confirmed_now for v in vs)
    assert "barge_grace_blocked" in _events(vs)
    # Continue past the grace: streak is already mature → confirms.
    v = g.on_frame(10.2, 5000, True, FRAME)
    assert v.confirmed_now


def test_off_quiet_frame_resets_streak():
    g = BargeGate(_cfg())
    g.on_tts_start(0.0)
    vs = _run(g, [5000, 5000, 100, 5000, 5000, 100])
    assert not any(v.confirmed_now for v in vs)
    assert _events(vs).count("barge_suppressed") == 2


def test_reset_unlatches_confirmed():
    g = BargeGate(_cfg())
    g.on_tts_start(0.0)
    _run(g, [5000, 5000, 5000])
    assert g.in_progress
    g.reset()
    assert g.state == GateState.IDLE and not g.in_progress


# ── Phase A — adaptive threshold ───────────────────────────────────────────────

def test_adaptive_uses_fixed_threshold_until_warmup():
    g = BargeGate(_cfg(adaptive_enabled=True, adaptive_warmup_frames=50))
    g.on_tts_start(0.0)
    _run(g, [30] * 49)                       # 49 noise frames < warmup
    assert g.threshold == 1200               # still the fixed cold-start
    _run(g, [30] * 1, t0=20.0)
    assert g.threshold == 600, "quiet line → clamps at adaptive_min"


def test_adaptive_rises_on_loud_background_and_caps():
    g = BargeGate(_cfg(adaptive_enabled=True, adaptive_warmup_frames=10))
    g.on_tts_start(0.0)
    _run(g, [300] * 60)                      # steady loud background (sub-threshold)
    # floor ≈ 300 → 7×300 = 2100, inside [600, 2600]
    assert 1900 <= g.threshold <= 2200, g.threshold
    _run(g, [1100] * 200, t0=30.0)           # hotter still (below current thr)
    assert g.threshold == 2600, "margin × floor must clamp at adaptive_max"


def test_adaptive_floor_not_poisoned_by_speech():
    g = BargeGate(_cfg(adaptive_enabled=True, adaptive_warmup_frames=10))
    g.on_tts_start(0.0)
    _run(g, [30] * 30)                       # quiet floor established
    thr_before = g.threshold
    _run(g, [5000, 5000], t0=20.0)           # over-threshold speech frames
    assert g.noise_floor < 100, "speech must not update the floor"
    assert g.threshold == thr_before


def test_adaptive_quiet_line_catches_soft_speech_fixed_missed():
    """The measured failure: soft speech ~800 RMS missed at fixed 1200 but
    caught once the adaptive floor drops the threshold to 600."""
    g = BargeGate(_cfg(adaptive_enabled=True, adaptive_warmup_frames=10))
    g.on_tts_start(0.0)
    _run(g, [30] * 20)                       # quiet line → threshold 600
    vs = _run(g, [800, 800, 800], t0=20.0)   # soft barge
    assert vs[2].confirmed_now


# ── Phase B1 — provisional window + stubbed confirm ────────────────────────────

def test_vad_confirm_m_of_n_flushes_buffered_onset():
    hits = iter([1, 1, 1])                   # VAD says speech immediately
    g = BargeGate(_cfg(confirm="local_vad"), confirm_frame=lambda f: next(hits, 1) == 1)
    g.on_tts_start(0.0)
    vs = _run(g, [5000, 5000, 5000, 5000, 5000])
    ev = _events(vs)
    assert "barge_provisional_open" in ev
    confirmed = [v for v in vs if v.confirmed_now]
    assert confirmed, "M-of-N speech frames must confirm"
    # Onset preserved: the confirming verdict flushes ALL provisional frames.
    assert len(confirmed[0].forward_frames) >= 3
    detail = dict(v_ for v in vs for v_ in v.events)["barge_confirmed"]
    assert detail["confirm"] == "local_vad" and "confirm_latency_ms" in detail


def test_vad_never_confirms_loud_nonspeech_abort_timeout():
    g = BargeGate(
        _cfg(confirm="local_vad", confirm_timeout_sec=0.08),
        confirm_frame=lambda f: False,       # VAD: never speech (fan/music-ish)
    )
    g.on_tts_start(0.0)
    vs = _run(g, [5000] * 10)                # sustained loud non-speech
    assert not any(v.confirmed_now for v in vs), "loud non-speech must not barge"
    assert "barge_abort" in _events(vs)
    detail = [d for v in vs for e, d in v.events if e == "barge_abort"][0]
    assert detail["reason"] == "timeout"
    # Sustained noise may legitimately re-open a new provisional window after
    # the abort — what matters is it NEVER confirms.
    assert g.state != GateState.CONFIRMED


def test_vad_energy_floor_aborts_early():
    g = BargeGate(
        _cfg(confirm="local_vad", confirm_energy_floor_frames=2),
        confirm_frame=lambda f: False,
    )
    g.on_tts_start(0.0)
    vs = _run(g, [5000, 5000, 5000, 100, 100])  # transient dies inside the window
    aborts = [d for v in vs for e, d in v.events if e == "barge_abort"]
    assert aborts and aborts[0]["reason"] == "energy_floor"
    assert not any(v.confirmed_now for v in vs)


def test_provisional_frames_are_not_forwarded_before_confirm():
    g = BargeGate(_cfg(confirm="local_vad", confirm_timeout_sec=0.08), confirm_frame=lambda f: False)
    g.on_tts_start(0.0)
    vs = _run(g, [5000] * 6)
    assert not any(v.forward_frames for v in vs), "no STT leakage from an unconfirmed window"


def test_tts_start_drops_stale_provisional():
    g = BargeGate(_cfg(confirm="local_vad"), confirm_frame=lambda f: False)
    g.on_tts_start(0.0)
    _run(g, [5000, 5000, 5000])              # opens provisional
    assert g.state == GateState.PROVISIONAL
    g.on_tts_start(50.0)                     # new agent turn
    assert g.state == GateState.IDLE and g.streak == 0


def test_missing_vad_degrades_to_tier1():
    g = BargeGate(_cfg(confirm="local_vad"), confirm_frame=None)  # webrtcvad unavailable
    assert g.cfg.confirm == "off"
    g.on_tts_start(0.0)
    vs = _run(g, [5000, 5000, 5000])
    assert vs[2].confirmed_now, "no VAD → Tier-1 semantics, never a dead gate"


# ── Transport integration (adaptive path through handle_text_frame) ───────────

def test_transport_adaptive_soft_barge_end_to_end(monkeypatch):
    """Quiet line → adaptive threshold drops to 600 → a soft ~800-RMS barge
    that fixed-1200 missed now cancels TTS, through the real transport path."""


    monkeypatch.setattr(settings, "barge_in_adaptive_enabled", True)
    monkeypatch.setattr(settings, "barge_in_adaptive_warmup_frames", 10)
    # This test pins the ENERGY layer's cut. The semantic layer above it
    # (full-duplex-semantic-barge-in) opens a yield window instead of cutting
    # on confirm — disabled here; it has its own tests (test_barge_semantics).
    monkeypatch.setattr(settings, "semantic_barge_enabled", False)

    def media(v):
        mulaw = audioop.lin2ulaw(struct.pack("<160h", *([v] * 160)), 2)
        return {"event": "media", "media": {"payload": base64.b64encode(mulaw).decode()}}

    class _Stt:
        async def send_audio(self, b): pass
        async def close(self): pass

    class _Aec:
        def process(self, b): return b
        def feed_reference(self, b): pass
        def discard_reference(self): pass

    class _S:
        config = {"assistant": {"workflow_id": "wf"}}
        call_id = "adaptive-e2e"
        def record_analytics(self, *a, **k): pass

    async def _noop(*a, **k): pass

    t = TwilioTransport(ws=None, send_raw=_noop, call_id="t-call")
    t._stt = _Stt()
    t._aec = _Aec()
    t._stream_sid = "MZ"
    t._recorder = None

    async def drive():
        t._tts_playing = True
        t._tts_started_at = 0.0  # grace long elapsed
        t._barge_gate.on_tts_start(0.0)
        for v in [30] * 20:            # quiet floor → threshold drops to 600
            await t.handle_text_frame(media(v))
        assert t._barge_gate.threshold == 600
        for v in (800, 800, 800):      # soft barge — below the old fixed 1200
            await t.handle_text_frame(media(v))

    asyncio.run(drive())
    assert t._tts_cancel.is_set(), "soft barge on a quiet line must cancel TTS"
    assert t._barge_gate.in_progress
