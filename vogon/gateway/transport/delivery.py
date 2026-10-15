"""Delivery reports — what actually reached the caller.

`send_response` historically returned None: all truncation knowledge
(barge-in cancel, chunks sent, drain-path clear) died inside the transport,
while all semantic state (node status, SPEAK_EMITTED, transcript) was
finalized at emission time. A DeliveryReport closes that one-way boundary so
the handler can log — and later act on — the delivered-vs-intended gap.

Pure module: no I/O, no transport imports. See
docs/speech-delivery-completion.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Words → milliseconds, the FALLBACK when nothing exact is available.
#
# DO NOT RETUNE WITHOUT MEASURING ON NUMBER-HEAVY TEXT. This value is known to
# be biased and is deliberately not the primary signal any more. On call
# 18b3f7a3, 96 words estimated 36.0s against 43.68s of real audio — 455 ms/word
# — because "$1,940.47" and "888-555-0199" are one word each and many spoken
# syllables. heard_fraction then clamped to 1.0 and a delivery cut at three
# sentences of four read as heard in full.
#
# The fix was to stop deciding on it, not to tighten it: in sentence mode the
# report carries sentences_sent/sentences_total (per-sentence marks Twilio
# echoes) and the arming gate uses those. This is reached only for burst mode
# and the widget, which count no sentences.
#
# Rationale and the incidents behind it: docs/voice-timing-constants.md §3
MS_PER_WORD_ESTIMATE = 375.0


def estimate_total_ms(text: str) -> float:
    """Estimated full playback duration of *text*, from word count."""
    words = len((text or "").split())
    return max(words, 1) * MS_PER_WORD_ESTIMATE


def estimate_heard_text(text: str, audio_ms: float | None, est_total_ms: float) -> str:
    """Best-effort prefix of *text* the caller heard, by audio proportion.

    Word-boundary aligned. An ESTIMATE — audio chunks do not map exactly to
    text — and always labelled as such where logged.
    """
    if audio_ms is None or est_total_ms <= 0:
        return ""
    fraction = min(1.0, max(0.0, audio_ms / est_total_ms))
    words = (text or "").split()
    return " ".join(words[: int(len(words) * fraction)])


@dataclass
class DeliveryReport:
    """Outcome of one send_response playout attempt.

    status:
      complete  — full stream sent, not cancelled (Twilio: mark may still be
                  pending; a later drain-clear is reported separately as a
                  DrainCleared event).
      truncated — barge-in cancelled the stream mid-flight.
      failed    — TTS error after retries.
    """
    audio_id: str
    status: Literal["complete", "truncated", "failed"]
    chunks_sent: int = 0
    audio_ms: float | None = None   # what was streamed; None if unknown (widget)
    est_total_ms: float = 0.0          # estimate from text length
    mark_pending: bool = False         # Twilio: playback mark not yet echoed
    # Bounded playout buffer (Phase 3, tts_pacing_mode="sentence"): exact
    # per-sentence accounting. heard_text = join of FULLY-SENT sentences —
    # with the 500 ms lead, sent ≈ played; consumers prefer it over the
    # proportional estimate when non-empty.
    sentences_total: int = 0
    sentences_sent: int = 0
    heard_text: str = ""
    # Metrics inversion (voice-gateway-phase0 §7): the transport reports raw
    # timings; the handler owns all metric/analytics emission.
    first_chunk_ms: float | None = None  # TTS first-chunk latency (client-measured)
    first_chunk_t: float | None = None   # monotonic when first audio headed to the caller

    @property
    def heard_fraction(self) -> float | None:
        """audio_ms / est_total_ms, clamped to [0, 1]. None if unknowable."""
        if self.audio_ms is None or self.est_total_ms <= 0:
            return None
        return min(1.0, self.audio_ms / self.est_total_ms)
