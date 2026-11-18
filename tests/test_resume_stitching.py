"""Resumed-turn transcript stitching (stt.py).

Production failure: eager EOT fired with the first part of the utterance,
TurnResumed arrived (caller kept talking), and the post-resume transcript
RESTARTED from the resume point — the turn committed with only the tail
fragment ("on the phone?") and the pre-resume words were lost. The handler's
processing half was correct (speculation cancelled, anchor restored); the
missing half was transcript continuity. The session now retains the
pre-resume transcript on TurnResumed and stitches it onto the post-resume
eager/final unless Flux already carried it forward.
"""
from __future__ import annotations

import asyncio

from deepgram.listen.v2.types import ListenV2TurnInfo
from deepgram.listen.v2.types.listen_v2turn_info_words_item import ListenV2TurnInfoWordsItem

from vogon.gateway.pipeline.stt import STTSession, merge_resumed_transcripts

# ── merge_resumed_transcripts (pure) ────────────────────────────────────────────

def test_cumulative_transcript_passes_through():
    # Flux behaved as documented: the new transcript already contains the prefix.
    assert merge_resumed_transcripts(
        "Can I pay it", "Can I pay it on the phone?",
    ) == "Can I pay it on the phone?"


def test_full_restart_concatenates():
    # The observed production case: post-resume transcript restarted.
    assert merge_resumed_transcripts(
        "Can I pay it", "on the phone?",
    ) == "Can I pay it on the phone?"


def test_partial_overlap_merges_once():
    assert merge_resumed_transcripts(
        "Can I pay it on", "on the phone?",
    ) == "Can I pay it on the phone?"


def test_containment_is_punctuation_insensitive():
    assert merge_resumed_transcripts(
        "can i pay it", "Can I pay it, on the phone?",
    ) == "Can I pay it, on the phone?"


def test_empty_sides():
    assert merge_resumed_transcripts("", "on the phone?") == "on the phone?"
    assert merge_resumed_transcripts("Can I pay it", "") == "Can I pay it"


# ── Session flow (attribute-level — no Deepgram connection) ─────────────────────

def _bare_session():
    s = object.__new__(STTSession)
    s._pending_text = ""
    s._pending_confidence = 0.0
    s._resumed_prefix = ""
    return s


def test_session_noop_when_flux_is_cumulative():
    s = _bare_session()
    s._resumed_prefix = "Can I pay it"
    assert s._apply_resume_stitching(
        "Can I pay it on the phone?"
    ) == "Can I pay it on the phone?"


def test_empty_final_flushes_retained_prefix():
    # An EndOfTurn with empty transcript while a prefix is retained must flush
    # the prefix (previously the event was skipped and the words lost).
    s = _bare_session()
    s._resumed_prefix = "Can I pay it"
    assert s._apply_resume_stitching("") == "Can I pay it"


# ── Session flow through the real message handler (Flux TurnInfo messages) ──────
# Until 2026-10-30 the two flows below re-enacted the TurnResumed transition by hand
# (s._resumed_prefix = merge_resumed_transcripts(...)); dropping the merge in
# STTSession._on_message would not have failed them.

def _turn_info(event: str, text: str, seq: int) -> ListenV2TurnInfo:
    words = [ListenV2TurnInfoWordsItem(word=word, confidence=0.95) for word in text.split()]
    return ListenV2TurnInfo(type="TurnInfo", request_id="r", sequence_id=seq, event=event, turn_index=0,
                            audio_window_start=0.0, audio_window_end=1.0, transcript=text, words=words,
                            end_of_turn_confidence=0.9)


def _flux(events: list[tuple[str, str]]) -> list[tuple]:
    """Feed the events through STTSession._on_message in eager mode; return what the
    callbacks saw, in order."""
    seen: list[tuple] = []

    async def interim(text, confidence):
        seen.append(("interim", text))

    async def final(text, confidence, elapsed_ms, low_conf_words):
        seen.append(("final", text))

    async def eager(text, confidence, elapsed_ms, low_conf_words):
        seen.append(("eager", text))

    async def resumed():
        seen.append(("resumed",))

    async def run():
        session = STTSession(interim, final, eager_eot_threshold=0.4, on_eager_end=eager, on_turn_resumed=resumed)
        for seq, (event, text) in enumerate(events):
            session._on_message(_turn_info(event, text, seq))
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)

    asyncio.run(run())
    return seen


def test_session_stitches_the_reference_call_sequence():
    """eager("Can I pay it") → TurnResumed → eager("on the phone?") → final must surface
    the FULL utterance at both the eager and the final."""
    seen = _flux([("EagerEndOfTurn", "Can I pay it"), ("TurnResumed", ""),
                  ("EagerEndOfTurn", "on the phone?"), ("EndOfTurn", "on the phone?")])
    assert seen == [("eager", "Can I pay it"), ("resumed",),
                    ("eager", "Can I pay it on the phone?"), ("final", "Can I pay it on the phone?")]


def test_session_double_resume_accumulates():
    seen = _flux([("EagerEndOfTurn", "Yeah so"), ("TurnResumed", ""),
                  ("EagerEndOfTurn", "can I pay"), ("TurnResumed", ""),
                  ("EndOfTurn", "it on the phone?")])
    assert seen[-1] == ("final", "Yeah so can I pay it on the phone?")
    assert ("eager", "Yeah so can I pay") in seen
