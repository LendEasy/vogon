"""Unit tests for the delivery-report pure module (Stage 1, shadow).

docs/speech-delivery-completion.md — DeliveryReport closes the one-way boundary
where truncation knowledge died inside the transport. These tests pin the pure
estimation/semantics; transport wiring is exercised by imports + the existing
suites (shadow mode is zero-behavior-change by design).
"""
from __future__ import annotations

import asyncio

from transport_fixtures import bare_twilio_transport

from vogon.gateway.transport.delivery import (
    MS_PER_WORD_ESTIMATE,
    DeliveryReport,
    estimate_heard_text,
    estimate_total_ms,
)
from vogon_contract import DrainCleared

# ── estimate_total_ms ───────────────────────────────────────────────────────────

def test_total_ms_scales_with_words():
    assert estimate_total_ms("one two three four") == 4 * MS_PER_WORD_ESTIMATE


def test_total_ms_empty_text_never_zero():
    # Guard: downstream divides by est_total_ms.
    assert estimate_total_ms("") == MS_PER_WORD_ESTIMATE
    assert estimate_total_ms("   ") == MS_PER_WORD_ESTIMATE


# ── estimate_heard_text ─────────────────────────────────────────────────────────

def test_heard_text_half_is_word_aligned_prefix():
    text = "one two three four five six seven eight"
    est = estimate_heard_text(text, audio_ms=4 * MS_PER_WORD_ESTIMATE,
                              est_total_ms=8 * MS_PER_WORD_ESTIMATE)
    assert est == "one two three four"
    assert text.startswith(est)  # always a clean prefix


def test_heard_text_full_delivery_returns_everything():
    text = "the whole line played"
    assert estimate_heard_text(text, 999999.0, estimate_total_ms(text)) == text


def test_heard_text_zero_audio_is_empty():
    assert estimate_heard_text("anything at all", 0.0, 1000.0) == ""


def test_heard_text_unknown_audio_is_empty():
    # Widget path: audio_ms is None (no fixed frame duration).
    assert estimate_heard_text("anything at all", None, 1000.0) == ""


def test_heard_text_bad_total_is_empty():
    assert estimate_heard_text("anything", 500.0, 0.0) == ""


# ── DeliveryReport.heard_fraction ───────────────────────────────────────────────

def test_heard_fraction_ratio_and_clamp():
    r = DeliveryReport(audio_id="a", status="truncated",
                       chunks_sent=10, audio_ms=500.0, est_total_ms=1000.0)
    assert r.heard_fraction == 0.5
    over = DeliveryReport(audio_id="a", status="complete",
                          chunks_sent=99, audio_ms=2000.0, est_total_ms=1000.0)
    assert over.heard_fraction == 1.0  # estimate ran under; clamp


def test_heard_fraction_unknowable_is_none():
    assert DeliveryReport(audio_id="a", status="truncated",
                          audio_ms=None, est_total_ms=1000.0).heard_fraction is None
    assert DeliveryReport(audio_id="a", status="complete",
                          audio_ms=100.0, est_total_ms=0.0).heard_fraction is None


# ── Transport contract ─────────────────────────────────────────────────────────


def test_drain_clear_emits_drain_cleared_event():
    """phase0 §4.2 (step 4): the drain-path late truncation is pushed as a
    DrainCleared event at the moment it happens (was: consume_drain_clear
    poll). FIFO puts it ahead of the barging caller's final."""


    t = bare_twilio_transport(_last_audio_id="resp-7", _stream_sid="MZ1")
    asyncio.run(t._clear_buffered_playback())

    events = []
    while not t._utterance_queue.empty():
        events.append(t._utterance_queue.get_nowait())
    drained = [e for e in events if isinstance(e, DrainCleared)]
    assert len(drained) == 1
    assert drained[0].audio_id == "resp-7"
    assert drained[0].at > 0


