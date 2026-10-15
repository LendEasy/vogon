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


def test_session_stitches_the_reference_call_sequence():
    """eager("Can I pay it") → TurnResumed → eager("on the phone?") → final
    must surface the FULL utterance at both the eager and the final."""
    s = _bare_session()

    # eager #1 (as the EagerEndOfTurn branch does)
    t1 = s._apply_resume_stitching("Can I pay it")
    s._pending_text = t1
    assert t1 == "Can I pay it"

    # TurnResumed: retain the pre-resume transcript
    s._resumed_prefix = merge_resumed_transcripts(s._resumed_prefix, s._pending_text)
    s._pending_text = ""

    # eager #2 arrives with a RESTARTED transcript (the bug's shape)
    t2 = s._apply_resume_stitching("on the phone?")
    s._pending_text = t2
    assert t2 == "Can I pay it on the phone?"   # speculation sees the full utterance

    # final EndOfTurn with the restarted transcript
    t3 = s._apply_resume_stitching("on the phone?")
    s._resumed_prefix = ""                       # EndOfTurn consumes the prefix
    assert t3 == "Can I pay it on the phone?"    # the turn commits FULL


def test_session_noop_when_flux_is_cumulative():
    s = _bare_session()
    s._resumed_prefix = "Can I pay it"
    assert s._apply_resume_stitching(
        "Can I pay it on the phone?"
    ) == "Can I pay it on the phone?"


def test_session_double_resume_accumulates():
    s = _bare_session()
    # resume 1 retained "Yeah so"
    s._resumed_prefix = merge_resumed_transcripts("", "Yeah so")
    # eager between resumes restarted: "can I pay"
    mid = s._apply_resume_stitching("can I pay")
    s._pending_text = mid
    assert mid == "Yeah so can I pay"
    # resume 2 retains the stitched pending
    s._resumed_prefix = merge_resumed_transcripts(s._resumed_prefix, s._pending_text)
    s._pending_text = ""
    final = s._apply_resume_stitching("it on the phone?")
    assert final == "Yeah so can I pay it on the phone?"


def test_empty_final_flushes_retained_prefix():
    # An EndOfTurn with empty transcript while a prefix is retained must flush
    # the prefix (previously the event was skipped and the words lost).
    s = _bare_session()
    s._resumed_prefix = "Can I pay it"
    assert s._apply_resume_stitching("") == "Can I pay it"
