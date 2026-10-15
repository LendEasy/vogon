"""Media-clock projection — the DURATION half of call timing (Phase 3).

docs/call-event-ordering.md §Phase 3 + full-duplex-turn-model.md §A. Ordering
(Phases 1-2) only needs a single monotonic coordinate. Cross-domain *duration*
questions — "did the caller's onset precede the audio finishing playing, and by
how much?" — need the observation latencies corrected, because our two ends
observe events late by different amounts:

  inbound  — the first STT interim ARRIVES ~100-300 ms after the caller
             actually started speaking. But Deepgram also gives each word a
             `.start` — seconds INTO the audio stream, stream-relative, not
             Deepgram wall-clock. Anchored to our stream epoch it recovers the
             true onset for free. `stream_to_monotonic` does that anchor.
  outbound — a Twilio mark echoes only after playout passes it, and the echo
             travels back over the network. The lead between our stream-end and
             the echo is a measured latency; `OffsetEstimator` tracks it so a
             duration query can estimate playout-complete for a speak whose mark
             has not echoed yet.

Skew-safe (docs §2): the stream epoch and every observed time are OUR single
monotonic clock; the foreign inputs are stream-relative deltas. No forward-clamp
merge, no vector clock — this module only projects, it does not order.

Pure: no I/O, no framework/transport imports. Unit-testable in isolation.
"""
from __future__ import annotations


def stream_to_monotonic(session_epoch: float, stream_s: float | None) -> float | None:
    """Project a stream-relative time (e.g. a Deepgram word `.start`, seconds
    into the audio) onto our monotonic clock.

    session_epoch: monotonic time the audio stream began (our clock).
    stream_s: seconds into that stream. None → None (no correction available;
    the caller falls back to its observation proxy).

    Skew-free: stream_s is a delta within a stream we sent, anchored on our own
    epoch — never a foreign wall-clock.
    """
    if stream_s is None:
        return None
    return session_epoch + stream_s


class OffsetEstimator:
    """A running estimate of a measured observation latency (e.g. the outbound
    jitter-buffer lead), maintained by an exponential moving average.

    Latencies vary turn to turn (jitter), so we track a smoothed value rather
    than assume a constant. Used to project one domain's observation onto the
    corrected media frame when a direct measurement is not yet available.
    """

    def __init__(self, alpha: float = 0.3) -> None:
        # alpha: EMA weight on the newest sample (0<alpha<=1). 0.3 = responsive
        # but not jumpy — a few samples to settle, quick to track a shift.
        self._alpha = alpha
        self._value: float | None = None
        self._count = 0

    def observe(self, seconds: float) -> None:
        """Fold in one measured latency sample."""
        if seconds < 0:
            return  # a negative lead is a measurement glitch — ignore
        if self._value is None:
            self._value = seconds
        else:
            self._value = self._alpha * seconds + (1 - self._alpha) * self._value
        self._count += 1

    def value(self) -> float:
        """Current smoothed latency in seconds; 0.0 before any sample."""
        return self._value if self._value is not None else 0.0

    @property
    def samples(self) -> int:
        return self._count

    def project_back(self, observed_t: float) -> float:
        """Correct an observed time by the estimated latency: an event OBSERVED
        at ``observed_t`` physically occurred ``value()`` seconds earlier."""
        return observed_t - self.value()
