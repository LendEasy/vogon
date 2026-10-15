"""Records both legs of a Twilio call and writes a stereo WAV at call end.

Both Twilio streams (inbound caller, outbound TTS) are mulaw 8kHz.  We tag
each chunk with a monotonic timestamp as it arrives/departs, then at call end
we place every chunk on a shared timeline and materialise a stereo 16-bit PCM
WAV (left = caller, right = agent).  No resampling is needed — both sides are
already 8 kHz mulaw, decoded to linear16 only at write time (lossless expansion).
"""
from __future__ import annotations

import array
import audioop
import io
import logging
import wave

from vogon.gateway.media_clock import stream_to_monotonic

logger = logging.getLogger(__name__)

# mulaw 8 kHz: 1 byte per sample, 8000 samples per second.
_SAMPLE_RATE = 8000
_SAMPLE_WIDTH = 2   # bytes per linear16 sample after ulaw2lin

# How far the stream clock may diverge from arrival time before the caller
# anchor is re-taken. Well above frame-scale jitter (measured +/-8-17 ms, and
# removing it is the point), far below anything a listener would hear as the
# two channels being out of turn. The exact value is a judgement, not a
# measurement; what makes it safe is that the anchor is self-correcting, so
# the bound only has to catch gross divergence.
_MAX_DRIFT_SEC = 0.25


class AudioRecorder:
    """Collects timestamped mulaw chunks from both call legs."""

    def __init__(self, stores=None) -> None:
        # (store, key_template) pairs — injected, defaulting to the shared
        # settings (artifact_store.default_recording_stores). The recorder
        # never reads settings/app.services itself (M1 item 8).
        self._stores = stores
        # Per-call policy from CallConfig.recording (the APPLICATION decides
        # IF; the driver sets this after start_call). Default True = today's
        # always-record behavior.
        self.enabled: bool = True
        self._t0: float | None = None
        self._caller_chunks: list[tuple[float, bytes]] = []
        self._agent_chunks: list[tuple[float, bytes]] = []
        # When the media stream began, per the transport. Sets the timeline
        # origin; not used to place frames (see set_stream_epoch).
        self._stream_epoch: float | None = None
        # Anchor for inbound frame placement: one frame's arrival paired with
        # that same frame's stream position (see caller_timestamp).
        self._stream_t0_mono: float | None = None
        self._stream_t0_ms: float = 0.0
        self._stream_ts_used: int = 0
        self._stream_ts_missing: int = 0
        self._stream_reanchors: int = 0
        self._stream_epoch_missing: bool = False
        # Largest signed (placed - arrival) seen; a diagnostic, not a control.
        self._stream_max_divergence: float = 0.0
        # (epoch-derived position - anchor-derived position) on the first
        # stamped frame: how well "start" lines up with stream time zero.
        self._stream_epoch_divergence: float | None = None

    # ── Inbound frame placement ───────────────────────────────────────────────

    def set_stream_epoch(self, monotonic_ts: float) -> None:
        """When the media stream began, on our monotonic clock.

        This sets the timeline ORIGIN. The stream begins when the call
        connects, before either party makes a sound, so an origin taken here
        does not depend on which leg happened to be captured first. Taking it
        from the first chunk fed instead is order-dependent: if the agent's
        greeting is recorded before the first caller frame, the greeting
        becomes the origin and any caller audio from earlier in the call
        computes a negative offset and is dropped. Anchoring on the stream
        start makes both legs non-negative by construction; the recording
        simply opens with whatever silence preceded the first sound.

        It is deliberately NOT used to place frames. Doing that would pair
        stream position zero with the moment we processed the "start" event,
        and the distance between those two — connection setup, handler
        scheduling — is unknown. `caller_timestamp` uses a pairing whose error
        is bounded by one frame's network lag instead. `_stream_epoch_divergence`
        records how far apart the two anchors actually sit, so that gap is a
        measurement on real calls rather than an assumption.
        """
        if self._stream_epoch is None:
            self._stream_epoch = monotonic_ts
        self._note_t0(monotonic_ts)

    def caller_timestamp(self, media: dict, arrival: float) -> float:
        """Where an inbound frame belongs on the timeline.

        Twilio stamps every media frame with `timestamp` — milliseconds since
        the stream began. That is when the audio was SPOKEN. Arrival time is
        when the frame reached us, which is the same thing plus a variable
        network delay, so placing frames by arrival scatters them by whatever
        that delay happened to be on each frame. Measured against Twilio's own
        recording of the same call, our caller leg correlated 0.97 at best and
        0.67 on average while the agent leg — already advancing on a sample
        clock — matched exactly. Every frame was present and correctly
        decoded; they were in slightly the wrong places, which is audible as
        roughness while the caller speaks.

        The stream clock is projected onto the monotonic clock rather than
        used directly, because `to_wav_bytes` places both legs against one
        shared `_t0`; a caller leg in stream time and an agent leg in
        monotonic time would be in different domains.

        The projection is anchored on one frame's arrival paired with that
        SAME frame's stream position. That pairing is self-correcting: a frame
        delayed on its way to us moves both halves together, so the delay
        cancels rather than offsetting everything after it. What it cannot
        absorb is the two clocks advancing at different RATES, since that
        accumulates — so when they disagree by more than `_MAX_DRIFT_SEC` the
        anchor is re-taken. Inside a drift window frames are evenly spaced;
        across windows the channel cannot walk away from real time. Re-anchors
        are counted, so the logs distinguish one settling event from a clock
        that is continuously wrong.

        A frame carrying no usable timestamp falls back to arrival time: a
        slightly misplaced frame beats a dropped one.
        """
        raw = (media or {}).get("timestamp")
        try:
            ms = float(raw)
        except (TypeError, ValueError):
            self._stream_ts_missing += 1
            return arrival
        if self._stream_t0_mono is None:
            self._stream_t0_mono = arrival
            self._stream_t0_ms = ms
            if self._stream_epoch is None:
                self._stream_epoch_missing = True
            else:
                # How far an epoch-anchored placement of this frame would
                # sit from where the arrival pairing puts it. Small means the
                # epoch is close to stream time zero and would be a viable
                # anchor; large means it is not. Reported, never acted on.
                self._stream_epoch_divergence = (
                    stream_to_monotonic(self._stream_epoch, ms / 1000.0) - arrival
                )
        self._stream_ts_used += 1
        placed = stream_to_monotonic(
            self._stream_t0_mono, (ms - self._stream_t0_ms) / 1000.0,
        )
        divergence = placed - arrival
        if abs(divergence) > abs(self._stream_max_divergence):
            self._stream_max_divergence = divergence
        if abs(divergence) > _MAX_DRIFT_SEC:
            # The two clocks have advanced at different rates — re-anchor so
            # the caller channel stays in step with the agent channel.
            self._stream_reanchors += 1
            self._stream_t0_mono = arrival
            self._stream_t0_ms = ms
            return arrival
        return placed

    # ── Public feed methods ───────────────────────────────────────────────────

    def add_caller_chunk(self, ts: float, data: bytes) -> None:
        if not data:
            return
        self._note_t0(ts)
        self._caller_chunks.append((ts, data))

    def add_agent_chunk(self, ts: float, data: bytes) -> None:
        if not data:
            return
        self._note_t0(ts)
        self._agent_chunks.append((ts, data))

    def discard_agent_after(self, ts: float) -> int:
        """Remove queued audio from the estimated recording when clear is sent.

        Twilio supplies no sample-level clear acknowledgement. This uses the
        dispatch time; network propagation can leave a short audible tail.
        Caller audio and already elapsed agent samples are preserved.
        """
        kept = []
        discarded = 0
        for start, data in self._agent_chunks:
            count = max(0, min(len(data), round((ts - start) * _SAMPLE_RATE)))
            discarded += len(data) - count
            if count:
                kept.append((start, data[:count]))
        self._agent_chunks = kept
        return discarded

    def _note_t0(self, ts: float) -> None:
        """t0 is the EARLIEST moment on the timeline, not the first one seen.

        Normally the stream epoch sets it and nothing is ever earlier. The
        minimum is kept as a backstop so that a call with no "start" event —
        or any future feed order — still cannot place a chunk before the
        origin, where it would be dropped by `_build_timeline`.
        """
        if self._t0 is None or ts < self._t0:
            self._t0 = ts

    # ── WAV construction ──────────────────────────────────────────────────────

    def to_wav_bytes(self) -> bytes | None:
        """Build and return a stereo 8kHz 16-bit PCM WAV, or None if no audio."""
        if not self._caller_chunks and not self._agent_chunks:
            return None

        t0 = self._t0 or 0.0

        # Determine total sample count: latest chunk end across both streams.
        total_samples = 0
        for ts, data in self._caller_chunks + self._agent_chunks:
            end = round((ts - t0) * _SAMPLE_RATE) + len(data)
            if end > total_samples:
                total_samples = end

        if total_samples == 0:
            return None

        caller_pcm = _build_timeline(self._caller_chunks, t0, total_samples)
        agent_pcm  = _build_timeline(self._agent_chunks,  t0, total_samples)

        # Interleave into stereo: L=caller (even indices), R=agent (odd indices).
        caller_arr = array.array("h")
        caller_arr.frombytes(caller_pcm)
        agent_arr = array.array("h")
        agent_arr.frombytes(agent_pcm)

        stereo = array.array("h", [0] * (total_samples * 2))
        stereo[0::2] = caller_arr
        stereo[1::2] = agent_arr

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(_SAMPLE_WIDTH)
            w.setframerate(_SAMPLE_RATE)
            w.writeframes(stereo.tobytes())

        return buf.getvalue()

    # ── Persistence ───────────────────────────────────────────────────────────

    @property
    def t0(self) -> float | None:
        """The recording's time origin (public accessor — tangle T8; the
        audio event log aligns its lanes to this)."""
        return self._t0

    async def save_and_upload(self, call_id: str) -> None:
        """Write the WAV to every configured artifact store. Never raises —
        errors are logged."""
        try:
            if not self.enabled:
                logger.info("AudioRecorder: recording disabled by policy for "
                            "call %s — skipping", call_id)
                return
            wav = self.to_wav_bytes()
            if wav is None:
                logger.info("AudioRecorder: no audio captured for call %s — skipping", call_id)
                return

            logger.info(
                "AudioRecorder: caller frames by stream clock=%d, arrival "
                "fallback=%d, re-anchors=%d, max stream-vs-arrival "
                "divergence=%+.0f ms, epoch-vs-anchor=%s",
                self._stream_ts_used, self._stream_ts_missing,
                self._stream_reanchors,
                self._stream_max_divergence * 1000,
                "no start event" if self._stream_epoch_divergence is None
                else "%+.0f ms" % (self._stream_epoch_divergence * 1000),
            )
            from vogon.gateway.audio import artifact_store  # noqa: PLC0415 — fail-open: inside the save path's except
            stores = (self._stores if self._stores is not None
                      else artifact_store.default_recording_stores())
            for store, key_template in stores:
                try:
                    await store.put(key_template.format(call_id=call_id),
                                    wav, "audio/wav")
                except Exception as exc:
                    logger.warning("AudioRecorder: store %s failed for call "
                                   "%s: %s", type(store).__name__, call_id, exc)
        except Exception as exc:
            logger.warning("AudioRecorder: failed for call %s: %s", call_id, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_timeline(chunks: list[tuple[float, bytes]], t0: float, total_samples: int) -> bytes:
    """Convert mulaw chunks to a zero-padded linear16 PCM byte array."""
    pcm = bytearray(total_samples * _SAMPLE_WIDTH)
    for ts, mulaw in chunks:
        # round, not truncate: a frame whose offset computes to 479.9999
        # (float error on an exact 480) would be placed one sample early, and
        # every such frame is a discontinuity against its neighbours.
        offset = round((ts - t0) * _SAMPLE_RATE)
        if offset < 0:
            # A negative index would write at the END of the buffer and
            # silently corrupt the tail. Cannot happen while t0 is the
            # earliest chunk, but a reordered or reset stream timestamp must
            # not be able to scramble a recording.
            logger.warning("AudioRecorder: dropping frame placed %d samples before t0", offset)
            continue
        linear = audioop.ulaw2lin(mulaw, _SAMPLE_WIDTH)
        byte_offset = offset * _SAMPLE_WIDTH
        end = byte_offset + len(linear)
        if end > len(pcm):
            pcm.extend(bytes(end - len(pcm)))
        pcm[byte_offset:end] = linear
    return bytes(pcm)

