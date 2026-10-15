"""Abstract transport interface — decouples I/O from call orchestration logic."""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

from vogon.gateway.transport.clocks import HoldReport
from vogon.gateway.transport.delivery import DeliveryReport


class BaseTransport(ABC):
    """Abstract transport layer for a call session.

    Decouples input delivery (STT vs typed text) and output delivery
    (TTS audio vs JSON text message) from the core agent orchestration logic.
    """

    @abstractmethod
    async def start(self) -> None:
        """Initialise the transport (e.g. open STT session). No-op for text mode."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down the transport."""

    @abstractmethod
    async def send_response(
        self, text: str, audio_id: str, protected: bool = False,
        absorb_barge: bool = False,
    ) -> DeliveryReport | None:
        """Deliver a response to the user.

        Voice: stream TTS audio with barge-in detection.
        Text: send a server.message JSON frame.

        protected: this playout must play to completion — barge-in never cuts
        it (intro/farewell protection, barge-in-compliance-protection.md).
        Safety phrases still cut; backchannels absorb; substantive caller
        speech queues and is handled right after the playout. Transports
        without barge machinery may ignore it.

        Returns a DeliveryReport describing what actually played (complete /
        truncated / failed), or None when there was nothing to deliver (empty
        text, no stream yet). See docs/speech-delivery-completion.md.
        """

    @abstractmethod
    async def handle_binary_frame(self, data: bytes) -> None:
        """Process an incoming binary WebSocket frame (PCM audio in voice mode; no-op in text)."""

    @abstractmethod
    async def handle_text_frame(self, msg: dict) -> bool:
        """Process an incoming text WebSocket frame.

        Returns True when the client has signalled a hangup.
        """

    @property
    @abstractmethod
    def utterance_queue(self) -> asyncio.Queue:
        """Async queue populated with user utterances as they arrive."""

    def next_event_seq(self) -> int:
        """Gateway-assigned monotonic event counter, per call (phase0 §4.2).

        Every contract event carries ``seq`` as an opaque ordering key the
        backend never generates. One counter per transport instance = per
        call. (Clock and delivery events are not yet stamped — they default
        to 0 until they cross the ProcessEventsRequest seam.)
        """
        self._event_seq = getattr(self, "_event_seq", 0) + 1
        return self._event_seq

    def playback_generation(self) -> int | None:
        floor = getattr(self, '_floor', None)
        return floor.generation if floor is not None else None

    async def send_response_guarded(self, text, audio_id, generation=None, **kwargs):
        floor = getattr(self, '_floor', None)
        if floor is not None and not await floor.allow_playback(generation, wait=True, audio_id=audio_id):
            return DeliveryReport(audio_id=audio_id, status='truncated', chunks_sent=0,
                                  audio_ms=0, est_total_ms=0, mark_pending=False)
        return await self.send_response(text, audio_id, **kwargs)

    def pause_silence(self) -> None:
        """A caller-turn request is outstanding: the reasoning gap is not the
        caller's silence. Pauses without resetting, so exit stays reachable."""
        clocks = getattr(self, "_clocks", None)
        if clocks is not None:
            clocks.pause_silence()

    def resume_silence(self) -> None:
        """The turn request finished — answered, failed, or gave up."""
        clocks = getattr(self, "_clocks", None)
        if clocks is not None:
            clocks.resume_silence()

    def grant_silence_grace(self, seconds: float) -> None:
        """The reasoning side acknowledged an explicit hold: the silence
        ladder waits `seconds` longer before probing. No-op on transports
        without clocks (widget/text)."""
        clocks = getattr(self, "_clocks", None)
        if clocks is not None:
            clocks.grant_silence_grace(seconds)

    async def wait_line_quiet(self) -> None:
        """Hold before a `play_after="line_quiet"` playout. Transports
        without a line detector play at once."""
        return None

    async def wait_playout_heard(self, cap_sec: float) -> bool:
        """Wait until audio already sent has been PLAYED to the far end —
        before an action that discards whatever the far end still buffers
        (a call redirect). Transports with no far-end buffer: at once."""
        return True

    async def send_held_response(self, text, audio_id, **kwargs):
        """A `play_after="line_quiet"` playout: wait for the line to be quiet,
        then play protected and absorb-all WITHOUT the floor controller's
        human turn-taking gate — the far end is a machine. Transports
        without a floor play at once."""
        await self.wait_line_quiet()
        return await self.send_response(text, audio_id, protected=True,
                                        absorb_barge=True)

    def select_silence_ladder(self, name: str) -> None:
        """The reasoning side named the silence ladder in force from now on
        ("default" | "opening", VOICEMAIL_DETECTION_DESIGN.md §12). No-op on
        transports without clocks."""
        clocks = getattr(self, "_clocks", None)
        if clocks is not None:
            clocks.select_ladder(name)

    def install_line_source(self, source, **knobs) -> bool:
        """Install the front model as the hold's source of words (transports
        with clocks only). Returns False when this transport has no clocks."""
        clocks = getattr(self, "_clocks", None)
        if clocks is None:
            return False
        clocks.install_line_source(source, **knobs)
        return True

    def install_hold_speak(self, speak) -> None:
        clocks = getattr(self, "_clocks", None)
        if clocks is not None:
            clocks.install_hold_speak(speak)

    def begin_hold(self) -> bool:
        """Start the filler window: the reasoning side is computing a turn.
        Returns True when a hold started (a FillerPolicy is configured on the
        transport's clocks; end_hold's report will be meaningful)."""
        clocks = getattr(self, "_clocks", None)
        return clocks.begin_hold() if clocks is not None else False

    async def end_hold(self) -> HoldReport:
        """End the filler window and report what played. An in-flight filler
        finishes naturally before this returns."""
        clocks = getattr(self, "_clocks", None)
        return await clocks.end_hold() if clocks is not None else HoldReport()

    def note_audio_event(self, event: str, **detail) -> None:
        """Record one audio-timeline event the DRIVER discovered.

        The seam's `events` verb, made public. Some facts on the audio
        timeline happen in the driver's own code and nowhere else — a barge
        cut that empties the outbound send queue is discovered by the send
        closure, not by the transport — and they belong in the SAME rebased
        audio log as the transport's own events, not in a second stream that
        the end-of-call flush never sees.

        The driver used to reach through `transport._audio_log` for exactly
        this, which is private transport access across the seam (pinned by
        tests/test_voice_pillar_imports.py and tests/test_pipeline_metrics.py).
        No-op on transports without an audio log (widget/text), same shape as
        the `_clocks` helpers above.
        """
        log = getattr(self, "_audio_log", None)
        if log is not None:
            log.emit(time.monotonic(), event, **detail)

    def transport_analytics(self) -> dict:
        """The gateway's end-of-call counters, as DATA.

        Read by the driver when it builds the ``EndCallRequest`` (phase0 §4.3:
        ``transport_analytics`` rides the request); the turn side writes the
        rows. The transport no longer reaches into the session to record them —
        that was a gateway → meaning write across the seam.

        Shape (JSON-representable, one entry per analytics row)::

            {"<event_name>": {"stage": str, "latency_ms": float,
                              "metadata": dict}}

        Every value is an accumulated counter, so this is safe to call after
        ``close()`` — the abnormal-exit path finalizes a closed transport.
        Empty for text mode, which has no STT or barge counters.
        """
        return {}
