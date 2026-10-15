"""
Acoustic Echo Canceller for Twilio 8 kHz phone calls.

Wraps SpeexDSP MDF AEC with GCC-PHAT echo delay estimation.

Validated (synthetic): 26.7 dB ERLE, 0% false barge-in triggers.
Validation harness and its record: docs/aec-validation-harness.md.

REFERENCE PACING
----------------
Twilio buffers TTS audio server-side and plays it out at real-time (8000 B/s).
Our TTS stream delivers chunks to Twilio faster than real-time (often 5–10×).
If we index the reference ring-buffer by position-since-last-send, the
reference and near-end advance at different rates, making the GCC-PHAT
delay estimate meaningless and the pre-shift lookup wrong.

Fix: `feed_reference` pushes to a FIFO; `process` pops exactly one 160-sample
frame per call. Both sides now advance at the inbound (real-time) rate.
`_shift_history` built inside `process` therefore maps frame N to what Twilio
was playing at near-end time N×20 ms.

RE-ALIGNMENT FROM THE AUDIO (2026-10-10)
----------------------------------------
One-per-call pairing is anchored on "the first inbound frame processed after
the line was sent", and that anchor can land a frame or more off what Twilio
actually played: a stall delivers frames spoken BEFORE the line after it was
sent (e2e-ladders-6272cade: five frames, the canceller's output rose to RMS 1823
over a raw 13-19, a false barge), a send landing on Twilio's tick is a coin flip
(e2e-ladders-f01258b1, the line's confirmation reached STT and the payment was
offered twice), a lost inbound frame slides the rest. One frame off is as fatal
as five (-1.2 dB, a run of 8 over the barge threshold).

No clock can settle it — Twilio never says when it played a frame, and pairing by
time on two 20 ms grids failed live (reverted, 0561f808). So the audio does:
every few frames, a window of the last 200 ms of near-end audio is cancelled
TRIAL-WISE at the current alignment and at its neighbours (+-1 frame, plus the
best of a +-8-frame correlation search for a stall), each trial a short
least-squares echo filter fitted to the window itself (no training time — Speex
has none to lend, and cannot be copied). The trial that leaves the least echo
wins when it clears a real echo (>= 6 dB) and beats the current alignment by a
margin (3 dB); the one Speex canceller is then shifted onto it, so the path it
learned on earlier lines is valid again at once — no reset. With no echo on the
line, or the caller talking over us, no trial clears the bar and nothing moves.
"""

from __future__ import annotations

import audioop
import collections
import logging
import math

import numpy as np
from speexdsp._speexdsp import EchoCanceller_create, EchoCanceller_process, delete_EchoCanceller

logger = logging.getLogger(__name__)

_FRAME_SAMPLES = 160  # 20 ms at 8 kHz — must match Twilio packet size
_SAMPLE_RATE = 8_000
_FILTER_TAIL_MS = 400  # SpeexDSP filter tail; must exceed max echo delay
_MAX_DELAY_MS = 500  # GCC-PHAT search window
_GCC_WARMUP_MS = 600  # accumulate this many ms before running GCC-PHAT
_FALLBACK_DELAY_MS = 180  # default pre-shift if GCC-PHAT fails
# The GCC-PHAT window opens at the first reference frame carrying speech, not
# at the first processed frame. The window used to open on whatever was being
# processed — at call start that is the inbound backlog before the opening's
# first TTS byte, i.e. a silent reference — and an all-silent window yields a
# flat correlation whose argmax is lag 0, which was then LOCKED for the call
# (every e2e duplex run, 2026-10-09: "AEC GCC-PHAT | delay_ms=0" against a
# 200 ms echo).
_GCC_ONSET_RMS = 200  # int16 RMS of a reference frame that opens the window
# Peak sharpness (peak / mean |PHAT| over the search window) an estimate must
# reach to be believed. Measured on synthetic speech (tests/aec/echo_path.py),
# 0.6 s windows, 10 signals: an echo at 0-450 ms and gain 0.1 scores 21-290;
# no echo (line noise, unrelated speech) scores 4.5-8.0.
# Below it the window is discarded and the next playout tries again.
_GCC_MIN_PEAK_RATIO = 12.0
# Re-alignment from the audio (see the module docstring). Measured on the real canceller,
# two lines at 200 ms / gain 0.3: slips of -1, +1, +3, +5, +7 frames corrected exactly, the
# filler at 19.7-25.7 dB after, no run over the barge threshold; aligned lines, a weak echo
# (0.05) and the caller over the line never moved; 0.15 ms per frame (0.03 without).
_REALIGN_WINDOW_FRAMES = 10   # 200 ms of near-end audio judged at a time
_REALIGN_EVERY_FRAMES = 5     # a judgement every 100 ms
_REALIGN_SEARCH_FRAMES = 8    # the correlation search for larger slips (a stalled backlog)
_REALIGN_TRIAL_TAPS = 16      # the trial canceller's length (2 ms): enough to tell lags apart
_REALIGN_MIN_ECHO_DB = 6.0    # a trial must remove this much to be an echo at all
_REALIGN_MARGIN_DB = 3.0      # and beat the current alignment by this much to move it
_SHIFT_MAXLEN = _MAX_DELAY_MS // 20 + 5 + _REALIGN_SEARCH_FRAMES + _REALIGN_WINDOW_FRAMES + 2

_ERLE_LOG_INTERVAL = 50  # log ERLE every N process() calls (~1 s)


class AECProcessor:
    """Real-time AEC for 8 kHz mulaw frames (Twilio phone calls).

    Call `feed_reference(mulaw_bytes)` for each TTS frame sent to Twilio.
    Call `process(near_mulaw)` for each inbound mulaw frame.
    Returns echo-cancelled mulaw bytes.

    Thread-safety: designed for single-threaded asyncio use only.
    """

    def __init__(self) -> None:
        # The speexdsp-python package ships a broken __init__.py that does
        # `from .speexdsp import *` but omits speexdsp.py from the installed
        # wheel. Import the C extension directly and define the wrapper here
        # so we are never dependent on the package including that file.

        class EchoCanceller:
            def __init__(
                self, frame_size: int, filter_length: int, sample_rate: int
            ) -> None:
                self._ptr = EchoCanceller_create(frame_size, filter_length, sample_rate)

            def process(self, near: bytes, ref: bytes) -> bytes:
                return EchoCanceller_process(self._ptr, near, ref)

            def __del__(self) -> None:
                if self._ptr is not None:
                    delete_EchoCanceller(self._ptr)
                    self._ptr = None

        filter_tail = _FILTER_TAIL_MS * _SAMPLE_RATE // 1000
        self._new_canceller = lambda: EchoCanceller(_FRAME_SAMPLES, filter_tail, _SAMPLE_RATE)
        self._ec = self._new_canceller()

        # Reference FIFO: fed at TTS burst rate, drained at inbound rate.
        self._ref_fifo: collections.deque[bytes] = collections.deque()

        # Shift history: populated at inbound rate by process().
        # Indexed -(delay_frames+1) to get the reference delayed by D frames.
        self._shift_history: collections.deque[bytes] = collections.deque(
            maxlen=_SHIFT_MAXLEN
        )

        self._delay_frames: int = 0
        self._delay_estimated: bool = False
        # frames the pairing is shifted by, found from the audio (RE-ALIGNMENT)
        self._alignment_slip: int = 0
        self._realign_near: collections.deque[bytes] = collections.deque(maxlen=_REALIGN_WINDOW_FRAMES)
        self._frames_since_realign_check: int = 0
        self.realignments: list[tuple[int, int, float, float]] = []   # (frame, shift, dB before, dB after)

        # GCC-PHAT accumulators (int16 PCM bytes at inbound rate)
        self._gcc_ref: bytearray = bytearray()
        self._gcc_near: bytearray = bytearray()
        self._gcc_target: int = _GCC_WARMUP_MS * _SAMPLE_RATE // 1000 * 2

        # ERLE tracking (rolling window)
        self._near_power_sum: float = 0.0
        self._out_power_sum: float = 0.0
        self._erle_frames: int = 0
        self._total_frames: int = 0

        logger.info(
            "AEC init | filter_tail_ms=%d max_delay_ms=%d warmup_ms=%d",
            _FILTER_TAIL_MS,
            _MAX_DELAY_MS,
            _GCC_WARMUP_MS,
        )

    # ── Public API ─────────────────────────────────────────────────────────

    def feed_reference(self, mulaw_bytes: bytes) -> None:
        """Called for each TTS mulaw frame sent to Twilio.

        Pushes to the reference FIFO; consumed one-per-frame by process().
        """
        pcm = _mulaw_to_pcm(mulaw_bytes)
        if pcm is not None:
            self._ref_fifo.append(pcm)

    def process(self, near_mulaw: bytes) -> bytes:
        """AEC-process a 20 ms near-end mulaw frame.

        Returns echo-cancelled mulaw. On any internal error, returns the
        original near_mulaw unchanged so the caller always gets audio.
        """
        self._total_frames += 1

        # 1. Pop next reference frame (paced to inbound clock)
        current_ref = self._ref_fifo.popleft() if self._ref_fifo else _SILENCE

        # 2. Advance shift history (one step per near-end frame)
        self._shift_history.append(current_ref)

        # 3. Decode near-end
        near_pcm = _mulaw_to_pcm(near_mulaw)
        if near_pcm is None:
            return near_mulaw

        # 4. Accumulate for GCC-PHAT: 600 ms from the reference's speech onset
        #    (contiguous once opened — skipping frames would scramble the lags)
        if not self._delay_estimated and (
            self._gcc_ref or audioop.rms(current_ref, 2) >= _GCC_ONSET_RMS
        ):
            self._gcc_ref.extend(current_ref)
            self._gcc_near.extend(near_pcm)
            if (
                len(self._gcc_ref) >= self._gcc_target
                and len(self._gcc_near) >= self._gcc_target
            ):
                self._run_gcc_phat()

        # 5. Look up pre-shifted reference
        delayed_ref = self._shifted_reference()

        # 6. Run AEC
        try:
            clean_pcm = self._ec.process(near_pcm, delayed_ref)
        except Exception as exc:
            logger.warning("AEC ec.process error: %s", exc)
            return near_mulaw

        # 7. Track ERLE
        self._track_erle(near_pcm, clean_pcm)

        # 8. Periodic ERLE log
        if self._erle_frames > 0 and self._erle_frames % _ERLE_LOG_INTERVAL == 0:
            self._log_erle()

        # 9. Re-alignment from the audio: judged on the window, applied from the next frame
        self._realign_near.append(near_pcm)
        self._frames_since_realign_check += 1
        if (self._delay_estimated and self._frames_since_realign_check >= _REALIGN_EVERY_FRAMES
                and len(self._realign_near) == _REALIGN_WINDOW_FRAMES):
            self._frames_since_realign_check = 0
            self._check_alignment()

        # 10. Encode output
        try:
            return audioop.lin2ulaw(clean_pcm, 2)
        except Exception:
            return near_mulaw

    def discard_reference(self) -> None:
        """Drop reference frames queued for playout that will never play.

        Called when the telco is told to discard its buffered audio (Twilio
        ``clear``). The FIFO models "what Twilio is playing now" by popping one
        frame per inbound frame; frames Twilio threw away must leave the FIFO
        too, or the next playout's reference lags the real audio by the
        discarded amount.
        """
        self._ref_fifo.clear()
        # both buffers are empty now: the next playout anchors afresh
        self._alignment_slip = 0

    def is_converged(self) -> bool:
        """True once GCC-PHAT has run and the pre-shift is set.

        Before this point the reference is misaligned and echo suppression is
        unreliable. Use this to flag turns that arrived before the filter was ready.
        """
        return self._delay_estimated

    def log_erle_now(self) -> None:
        """Force an ERLE log entry immediately (e.g. at barge-in or TTS end)."""
        self._log_erle()

    # ── Internal helpers ────────────────────────────────────────────────────

    def _shifted_reference(self) -> bytes:
        if not self._shift_history:
            return _SILENCE
        lag = self._delay_frames + self._alignment_slip
        if lag == 0:
            # GCC-PHAT not yet run (or found zero delay): use the most recent
            # reference so SpeexDSP has something to adapt on immediately.
            return self._shift_history[-1]
        idx = -(lag + 1)
        if lag < 0 or abs(idx) > len(self._shift_history):
            return _SILENCE  # history not deep enough yet
        return self._shift_history[idx]

    # ── re-alignment from the audio (module docstring) ──────────────────────

    def _reference_run(self, lag: int) -> np.ndarray | None:
        """The reference samples the window's near-end frames are paired with at
        `lag` frames, preceded by the trial filter's memory; None outside the history."""
        frames = []
        for position in range(-1, _REALIGN_WINDOW_FRAMES):
            index = -(_REALIGN_WINDOW_FRAMES - position) - lag
            if lag < 0 or index >= 0 or abs(index) > len(self._shift_history):
                return None
            frames.append(self._shift_history[index])
        samples = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float64)
        return samples[_FRAME_SAMPLES - (_REALIGN_TRIAL_TAPS - 1):]

    def _trial_cancellation_db(self, near: np.ndarray, lag: int) -> float | None:
        """Echo removed (dB) by the best short echo filter at `lag`, fitted to the
        window itself by least squares; None when there is no reference to fit."""
        reference = self._reference_run(lag)
        if reference is None or float(reference @ reference) < 1.0:
            return None
        matrix = np.lib.stride_tricks.sliding_window_view(reference, _REALIGN_TRIAL_TAPS)[:, ::-1]
        fit, *_ = np.linalg.lstsq(matrix, near, rcond=None)
        residual = near - matrix @ fit
        return 10.0 * math.log10(float(near @ near) / max(float(residual @ residual), 1e-9))

    def _correlation_lag(self, near: np.ndarray, current: int) -> int | None:
        """The lag within +-_REALIGN_SEARCH_FRAMES whose reference correlates best
        with the window: a stall can steal more frames than the neighbours cover."""
        best_lag, best_correlation = None, 0.0
        for offset in range(-_REALIGN_SEARCH_FRAMES, _REALIGN_SEARCH_FRAMES + 1):
            reference = self._reference_run(current + offset)
            if reference is None:
                continue
            aligned = reference[_REALIGN_TRIAL_TAPS - 1:]
            norm = math.sqrt(float(aligned @ aligned) * float(near @ near))
            if norm > 0:
                correlation = abs(float(aligned @ near)) / norm
                if correlation > best_correlation:
                    best_lag, best_correlation = current + offset, correlation
        return best_lag

    def _check_alignment(self) -> None:
        """Cancel the window trial-wise at the current alignment, its neighbours and
        the correlation search's best; shift onto the winner when it is clearly better."""
        try:
            current = self._delay_frames + self._alignment_slip
            reference = self._reference_run(current)
            if reference is None or float(np.sqrt(np.mean(reference[_REALIGN_TRIAL_TAPS - 1:] ** 2))) < _GCC_ONSET_RMS:
                return    # we were not speaking in this window: nothing to judge by
            near = np.frombuffer(b"".join(self._realign_near), dtype=np.int16).astype(np.float64)
            candidates = {current - 1, current, current + 1, self._correlation_lag(near, current)} - {None}
            scores = {lag: self._trial_cancellation_db(near, lag) for lag in candidates}
            scores = {lag: score for lag, score in scores.items() if score is not None}
            if current not in scores:
                return
            best = max(scores, key=scores.get)
            if best == current or scores[best] < _REALIGN_MIN_ECHO_DB or scores[best] < scores[current] + _REALIGN_MARGIN_DB:
                return
            shift = best - current
            self._alignment_slip += shift
            self.realignments.append((self._total_frames, shift, round(scores[current], 1), round(scores[best], 1)))
            logger.info("AEC realigned | shift_frames=%+d slip_frames=%+d trial_db %.1f -> %.1f",
                        shift, self._alignment_slip, scores[current], scores[best])
        except Exception as exc:  # noqa: BLE001 — the canceller keeps running on its current alignment
            logger.warning("AEC re-alignment check failed: %s", exc)

    def _run_gcc_phat(self) -> None:
        try:
            n = min(len(self._gcc_ref), len(self._gcc_near), self._gcc_target)
            ref = np.frombuffer(bytes(self._gcc_ref[:n]), dtype=np.int16).astype(
                np.float32
            )
            near = np.frombuffer(bytes(self._gcc_near[:n]), dtype=np.int16).astype(
                np.float32
            )

            fft_n = len(ref) + len(near)
            # near first: peak at +D (not -D)
            R = np.fft.rfft(near, n=fft_n) * np.conj(np.fft.rfft(ref, n=fft_n))
            denom = np.abs(R)
            denom[denom < 1e-10] = 1e-10
            phat = np.fft.irfft(R / denom, n=fft_n)

            max_lag = _MAX_DELAY_MS * _SAMPLE_RATE // 1000
            search_window = np.abs(phat[:max_lag])
            delay_samples = int(np.argmax(search_window))
            mean_magnitude = float(search_window.mean())
            peak_ratio = (
                float(search_window[delay_samples]) / mean_magnitude
                if mean_magnitude > 0
                else 0.0
            )
            delay_ms = delay_samples * 1000 // _SAMPLE_RATE
            if peak_ratio < _GCC_MIN_PEAK_RATIO:
                # No echo the correlation can see (silent near end, a phone
                # that cancels its own echo, caller speech only). Do not lock
                # a guess: keep the current alignment and retry on the next
                # window of reference speech.
                logger.info(
                    "AEC GCC-PHAT inconclusive | peak_ratio=%.1f < %.1f"
                    " candidate_delay_ms=%d — retrying on the next reference speech",
                    peak_ratio,
                    _GCC_MIN_PEAK_RATIO,
                    delay_ms,
                )
                return
            self._delay_frames = max(0, delay_samples // _FRAME_SAMPLES)
            self._delay_estimated = True
            if self._delay_frames:
                # The filter adapted so far modelled the echo against the
                # UNSHIFTED reference; after the pre-shift every tap is off by
                # delay_frames, and unlearning that cost ~10 dB of ERLE for
                # the next 1.4 s (measured: 6.5 → 16 dB at 200 ms, 0.6–2 s).
                # Start the filter fresh on the aligned reference.
                self._ec = self._new_canceller()
            logger.info(
                "AEC GCC-PHAT | delay_ms=%d delay_frames=%d peak_ratio=%.1f",
                delay_ms,
                self._delay_frames,
                peak_ratio,
            )
        except Exception as exc:
            self._delay_frames = _FALLBACK_DELAY_MS // 20
            self._delay_estimated = True
            logger.warning(
                "AEC GCC-PHAT failed → fallback %d ms: %s",
                _FALLBACK_DELAY_MS,
                exc,
            )
        finally:
            self._gcc_ref = bytearray()
            self._gcc_near = bytearray()

    def _track_erle(self, near_pcm: bytes, clean_pcm: bytes) -> None:
        try:
            nr = audioop.rms(near_pcm, 2)
            or_ = audioop.rms(clean_pcm, 2)
            self._near_power_sum += nr * nr
            self._out_power_sum += or_ * or_
            self._erle_frames += 1
        except Exception:
            pass

    def _log_erle(self) -> None:
        if self._erle_frames < 5 or self._out_power_sum <= 0:
            return
        near_p = self._near_power_sum / self._erle_frames
        out_p = self._out_power_sum / self._erle_frames
        if near_p > 0 and out_p > 0:
            erle_db = 10.0 * math.log10(near_p / out_p)
            logger.debug(
                "AEC ERLE | erle_db=%.1f delay_frames=%d ref_fifo=%d total_frames=%d",
                erle_db,
                self._delay_frames,
                len(self._ref_fifo),
                self._total_frames,
            )
        self._near_power_sum = 0.0
        self._out_power_sum = 0.0
        self._erle_frames = 0


# ── Module-level helpers ──────────────────────────────────────────────────────

_SILENCE = bytes(_FRAME_SAMPLES * 2)  # 320 zero bytes (int16 PCM silence)


def _mulaw_to_pcm(mulaw_bytes: bytes) -> bytes | None:
    """Decode mulaw → int16 PCM, padded/clamped to exactly FRAME_SAMPLES."""
    try:
        pcm = audioop.ulaw2lin(mulaw_bytes, 2)
    except Exception as exc:
        logger.debug("AEC mulaw decode error: %s", exc)
        return None
    target = _FRAME_SAMPLES * 2
    if len(pcm) < target:
        pcm = pcm + bytes(target - len(pcm))  # zero-pad tail
    elif len(pcm) > target:
        pcm = pcm[:target]
    return pcm


def create_aec() -> AECProcessor | None:
    """Create an AECProcessor, or return None if speexdsp is not installed."""
    try:
        proc = AECProcessor()
        return proc
    except ImportError:
        logger.warning(
            "speexdsp not installed — AEC disabled. "
            "Install: pip install git+https://github.com/xiongyihui/speexdsp-python.git"
        )
        return None
    except Exception as exc:
        logger.warning("AEC init failed: %s", exc)
        return None
