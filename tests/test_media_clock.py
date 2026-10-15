"""§Phase 3 media-clock projection (docs/call-event-ordering.md, media_clock.py).

The DURATION half: correct the observation latencies so cross-domain timing is
accurate. Two pure pieces:
  - stream_to_monotonic: anchor a Deepgram word .start (stream-relative) onto
    our monotonic clock — the onset correction (its biggest single win).
  - OffsetEstimator: an EMA of a measured latency (outbound jitter lead), for
    projecting a still-in-flight playout onto the media frame.

Plus the STT-level integration: first-word .start → true onset on our clock.
"""
from __future__ import annotations

from vogon.gateway.media_clock import OffsetEstimator, stream_to_monotonic
from vogon.gateway.pipeline.stt import STTSession

# ── stream_to_monotonic (onset anchor) ──────────────────────────────────────────

def test_stream_to_monotonic_anchors_word_start():
    # Session opened at monotonic 1000.0; the word starts 3.42s into the stream.
    assert stream_to_monotonic(1000.0, 3.42) == 1000.0 + 3.42


def test_stream_to_monotonic_none_when_no_word_timing():
    assert stream_to_monotonic(1000.0, None) is None


def test_onset_correction_is_earlier_than_the_interim_proxy():
    """The whole point: word .start (true onset) precedes the first-interim
    arrival (proxy) by the STT delivery latency."""
    epoch = 1000.0
    true_onset = stream_to_monotonic(epoch, 5.00)     # word started 5.00s in
    interim_arrival_proxy = epoch + 5.25              # interim reached us 250ms later
    assert true_onset < interim_arrival_proxy
    assert round(interim_arrival_proxy - true_onset, 3) == 0.25


# ── OffsetEstimator (outbound jitter lead) ──────────────────────────────────────

def test_estimator_starts_at_zero():
    e = OffsetEstimator()
    assert e.value() == 0.0 and e.samples == 0


def test_estimator_ema_tracks_and_smooths():
    e = OffsetEstimator(alpha=0.5)
    e.observe(0.6)                 # first sample = value
    assert e.value() == 0.6
    e.observe(0.4)                 # 0.5*0.4 + 0.5*0.6
    assert abs(e.value() - 0.5) < 1e-9
    assert e.samples == 2


def test_estimator_ignores_negative_glitches():
    e = OffsetEstimator()
    e.observe(0.5)
    e.observe(-1.0)                # measurement glitch — dropped
    assert e.value() == 0.5 and e.samples == 1


def test_project_back_corrects_by_latency():
    e = OffsetEstimator()
    e.observe(0.6)
    # A playout OBSERVED complete at t=100 physically finished ~0.6s earlier.
    assert abs(e.project_back(100.0) - 99.4) < 1e-9


# ── STT integration: first-word .start → true onset ─────────────────────────────

class _Word:
    def __init__(self, start, conf=0.99, word="w"):
        self.start = start
        self.confidence = conf
        self.word = word


def test_stt_last_onset_monotonic_from_first_word():
    async def _noop(*a, **k):
        pass

    s = STTSession.__new__(STTSession)      # skip network __init__
    s._session_start = 2000.0
    s._last_onset_stream_s = None
    # No word timing yet → no correction.
    assert s.last_onset_monotonic() is None
    # After a turn captured the first word's .start …
    s._last_onset_stream_s = 4.1
    assert s.last_onset_monotonic() == 2000.0 + 4.1
