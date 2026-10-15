"""Full-duplex semantic barge-in — Phase 1 pure verdict logic.

docs/full-duplex-semantic-barge-in.md §4. Decisions under test (user, 2026-07-15):
absorb silently + BARGE_ABSORBED; ambiguous-or-timeout → ABSORB; absorption
swallows the turn unless the final grew substantive.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import struct

from vogon.gateway.settings import GatewaySettings as Settings
from vogon.gateway.settings import settings
from vogon.gateway.transport.barge_semantics import (
    ABSORB_VERDICTS,
    CUT_VERDICTS,
    BargeVerdictSignal,
    heuristic_verdict,
    is_absorb,
    resolve_final,
    should_swallow_final,
)
from vogon.gateway.transport.twilio_transport import TwilioTransport
from vogon_contract import UtteranceAbsorbed

# The break-through set is APPLICATION-supplied since M1 item 9 (the gateway
# ships no domain phrases) — tests pass it explicitly, mirroring what
# lifecycle puts on CallConfig.barge_policy.
PHRASES = ("stop calling", "don't call", "do not call", "take me off",
           "wrong number", "wrong person", "attorney", "lawyer", "sue you")

# ── heuristic_verdict (the 0 ms floor) ─────────────────────────────────────────

def test_no_text_is_noise():
    assert heuristic_verdict("") == "no_text"
    assert heuristic_verdict("   ") == "no_text"


def test_backchannels_absorb():
    for t in ("okay", "Yeah.", "uh-huh", "yeah okay", "got it", "Right",
              "mm-hmm", "sure sure"):
        assert heuristic_verdict(t) == "backchannel", t


def test_safety_cuts_immediately_without_llm():
    for t in ("stop calling me", "you've got the wrong number",
              "I'm going to talk to my lawyer about this"):
        assert heuristic_verdict(t, safety_phrases=PHRASES) == "safety", t


def test_no_phrases_supplied_means_no_safety_verdict():
    # A generic deployment that configures no break-through set gets the
    # normal verdict ladder, never a phantom safety cut.
    assert heuristic_verdict("stop calling me") != "safety"


def test_substantive_cuts():
    # Long utterance
    assert heuristic_verdict(
        "can you tell me what the details on this loan are"
    ) == "substantive_interrupt"
    # Question-shaped, even when short
    assert heuristic_verdict("how much?") == "substantive_interrupt"


def test_middle_ground_is_ambiguous_for_the_llm():
    # Short but not lexicon-only — e.g. a partial ZIP answer mid-line.
    assert heuristic_verdict("it's nine eight one") == "ambiguous"
    assert heuristic_verdict("yes it's a good time") == "ambiguous"


# ── resolve_final (verdict, source composition) ─────────────────────────────────

def test_decisive_heuristic_wins():
    assert resolve_final("backchannel", None) == ("backchannel", "heuristic")
    assert resolve_final("safety", None) == ("safety", "heuristic")
    assert resolve_final("substantive_interrupt", None) == ("substantive_interrupt", "heuristic")


def test_llm_decides_the_ambiguous_middle():
    assert resolve_final("ambiguous", "answer_to_heard") == ("answer_to_heard", "llm")
    assert resolve_final("ambiguous", "substantive_interrupt") == ("substantive_interrupt", "llm")
    assert resolve_final("ambiguous", "backchannel") == ("backchannel", "llm")


def test_ambiguous_without_llm_absorbs_by_default():
    # USER DECISION: when unsure (timeout / error), keep speaking.
    verdict, source = resolve_final("ambiguous", None)
    assert verdict == "timeout_absorb"
    assert source == "window_expiry"
    assert is_absorb(verdict)


def test_no_text_expiry_absorbs_as_noise():
    assert resolve_final("no_text", None) == ("no_text", "window_expiry")


# ── absorb/cut partition ────────────────────────────────────────────────────────

def test_verdict_partition_is_total_and_disjoint():
    assert ABSORB_VERDICTS & CUT_VERDICTS == frozenset()
    for v in ABSORB_VERDICTS:
        assert is_absorb(v)
    for v in CUT_VERDICTS:
        assert not is_absorb(v)


# ── should_swallow_final (§4.5 — absorption swallows the turn) ──────────────────

def test_backchannel_final_is_swallowed():
    assert should_swallow_final("Okay.") is True
    assert should_swallow_final("yeah uh-huh") is True


def test_final_grown_substantive_is_processed_normally():
    # Caller kept talking past the backchannel into real content: the absorb
    # spared the audio, but the content must still drive a turn.
    assert should_swallow_final(
        "okay but can you explain what the late fee was for"
    ) is False


def test_ambiguous_final_stays_swallowed():
    # The window judged it absorbable; an ambiguous final doesn't overturn that.
    assert should_swallow_final("it's nine eight one") is True


# ── plumbing contracts ──────────────────────────────────────────────────────────

def test_verdict_schema_shape():
    s = BargeVerdictSignal(verdict="backchannel", confidence=0.9)
    assert s.verdict == "backchannel"


def test_absorbed_event_contract_shape():
    """An absorb decision always names its phase, verdict and source — all
    required by contract; audit fields default to zero values."""
    e = UtteranceAbsorbed(text="hello", phase="final",
                          verdict="backchannel", source="heuristic")
    assert e.phase == "final" and e.verdict == "backchannel"
    assert e.window_ms == 0.0 and e.audio_id == "" and e.final_text == ""


def test_config_defaults_enabled():
    f = Settings.model_fields
    assert f["semantic_barge_enabled"].default is True
    assert f["semantic_barge_window_ms"].default == 900
    assert f["semantic_barge_llm_timeout_ms"].default == 700


# ── Transport end-to-end: confirm opens the window; verdict drives the action ──

def _transport(monkeypatch):
    """Real TwilioTransport driven with synthetic frames (same harness as
    test_barge_gate's end-to-end test)."""

    monkeypatch.setattr(settings, "semantic_barge_enabled", True)
    monkeypatch.setattr(settings, "barge_controller_enabled", False)  # legacy window regression
    monkeypatch.setattr(settings, "semantic_barge_window_ms", 200)  # fast tests
    monkeypatch.setattr(settings, "semantic_barge_llm_timeout_ms", 100)

    class _Stt:
        async def send_audio(self, b): pass

    class _Aec:
        def process(self, b): return b
        def feed_reference(self, b): pass
        def discard_reference(self): pass

    class _S:
        config = {"assistant": {"workflow_id": "wf"}}
        call_id = "semantic-e2e"
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


def _drive_confirm(t, interim_text):
    """Feed frames to energy-confirm, seed the interim text, run the window."""

    async def go():
        t._tts_playing = True
        t._tts_started_at = 0.0  # grace long elapsed
        t._last_speak_text = "Is now a good time to speak? Before we continue I need to verify your identity."
        t._barge_gate.on_tts_start(0.0)
        t._last_interim_text = interim_text
        for v in (2000, 2000, 2000):   # over the fixed 1200 threshold
            await t.handle_text_frame(_media(v))
        assert t._yield_task is not None, "confirm must open the yield window"
        assert not t._tts_cancel.is_set(), "no cut at confirm time — window first"
        await asyncio.wait_for(t._yield_task, timeout=2.0)

    asyncio.run(go())


def test_e2e_backchannel_absorbs_and_swallows(monkeypatch):
    t = _transport(monkeypatch)
    _drive_confirm(t, "okay")
    assert not t._tts_cancel.is_set()          # audio never cut
    assert t._absorbing_utterance is True      # turn will be swallowed
    assert t._absorb_detail["verdict"] == "backchannel"
    assert not t._barge_gate.in_progress       # gate re-armed for a real barge


def test_e2e_substantive_cuts(monkeypatch):
    t = _transport(monkeypatch)
    _drive_confirm(t, "wait can you explain what this late fee actually is")
    assert t._tts_cancel.is_set()              # in-flight cut
    assert t._absorbing_utterance is False


def test_e2e_no_text_expiry_absorbs_noise(monkeypatch):
    # Energy-confirmed but STT produced nothing (cough/noise): pre-feature this
    # cleared real audio; now it absorbs.
    t = _transport(monkeypatch)
    _drive_confirm(t, "")
    assert not t._tts_cancel.is_set()
    assert t._absorbing_utterance is True
    assert t._absorb_detail["verdict"] == "no_text"
