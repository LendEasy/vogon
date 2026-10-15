"""Sentence segmentation for the bounded playout buffer (Phase 3).

docs/full-duplex-semantic-barge-in.md §6.2a. Splits the agent's OWN generated
speak text into sentences so TTS can stream per-sentence segments (exact
delivered/heard tracking; graceful barge yields at sentence boundaries).

Deliberately conservative and mechanical: this is OUR text, not caller speech
(the no-regex-for-speech rule governs understanding callers, not pacing our
own output). Rules:
- split only after sentence-final . ! ? followed by whitespace — never inside
  amounts ($201.42), dates (2026-06-01 has no dot), or mid-token;
- fragments shorter than MIN_SENTENCE_CHARS merge forward into the next piece
  (an occasional false split like "Dr." costs one merge, never a broken line);
- a single-sentence or unsplittable text returns [text].

Defaults (documented in README + full-duplex-semantic-barge-in §6):
MIN_SENTENCE_CHARS = 25.
"""
from __future__ import annotations

import re

MIN_SENTENCE_CHARS = 25

# After sentence-final punctuation + whitespace. The lookbehind requires the
# punctuation; the whitespace requirement is what keeps "$201.42" intact (its
# dot is followed by digits, not whitespace).
_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str, min_chars: int = MIN_SENTENCE_CHARS) -> list[str]:
    """Split *text* into sentence segments for per-segment TTS streaming."""
    stripped = (text or "").strip()
    if not stripped:
        return []
    raw = [p.strip() for p in _BOUNDARY.split(stripped) if p.strip()]
    if len(raw) <= 1:
        return [stripped]
    # Merge short fragments forward so tiny pieces ("Okay.", a false "Dr."
    # split) never become their own segment/mark.
    merged: list[str] = []
    for piece in raw:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    # A short trailing fragment merges backward.
    if len(merged) >= 2 and len(merged[-1]) < min_chars:
        merged[-2] = f"{merged[-2]} {merged[-1]}"
        merged.pop()
    return merged
