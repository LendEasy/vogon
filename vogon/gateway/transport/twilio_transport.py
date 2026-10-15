"""Twilio Media Streams transport — bridges Twilio phone calls into the existing pipeline.

Wire format
===========
Twilio sends JSON text frames over the WebSocket (never binary):

  {"event": "connected", ...}
  {"event": "start", "streamSid": "MZ...", "start": {"callSid": "CA...", ...}}
  {"event": "media", "media": {"payload": "<base64 mulaw 8kHz>"}, "streamSid": "MZ..."}
  {"event": "stop", ...}

We send audio back as:

  {"event": "media", "streamSid": "MZ...", "media": {"payload": "<base64 mulaw 8kHz>"}}

To interrupt buffered audio already queued on Twilio's side:

  {"event": "clear", "streamSid": "MZ..."}

Audio conversion
================
- Inbound (Twilio → STT): mulaw 8kHz passed directly to Deepgram (encoding=mulaw, sample_rate=8000).
- Outbound (TTS → Twilio) — two paths, controlled by settings.tts_phone_encoding:

  Phone-optimised path (default, tts_phone_encoding="mulaw"):
    Deepgram TTS outputs mulaw 8kHz directly (container="none", no WAV wrapper).
    No Python-side resampling. Frames buffered to _TWILIO_TTS_FRAME_BYTES before
    sending to reduce fragmentation.

  Legacy fallback path (tts_phone_encoding="linear16"):
    Deepgram TTS outputs linear16 24kHz → audioop.ratecv → audioop.lin2ulaw → mulaw 8kHz.
    Set only for debugging; produces aliasing artefacts from the unfiltered downsample.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import csv
import json
import logging
import math
import time
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import WebSocket

from vogon.gateway.audio.audio_recorder import AudioRecorder
from vogon.gateway.media_clock import OffsetEstimator
from vogon.gateway.metrics.sink import metrics_sink
from vogon.gateway.pipeline.sentence_split import split_sentences
from vogon.gateway.pipeline.stt import STTSession
from vogon.gateway.pipeline.tts import TTSClient
from vogon.gateway.settings import settings
from vogon.gateway.transport.aec import AECProcessor, create_aec
from vogon.gateway.transport.ambience import AmbienceConfig, TypingState, build_mixer
from vogon.gateway.transport.audio_event_log import AudioEventLog
from vogon.gateway.transport.barge_gate import BargeGate, BargeGateConfig, make_confirm_frame
from vogon.gateway.transport.barge_semantics import (
    DECISIVE_HEURISTICS,
    classify_barge,
    heuristic_verdict,
    is_absorb,
    is_echo_of,
    resolve_final,
    should_swallow_absorbed_final,
)
from vogon.gateway.transport.base import BaseTransport
from vogon.gateway.transport.clocks import CallClocks
from vogon.gateway.transport.delivery import DeliveryReport, estimate_heard_text, estimate_total_ms
from vogon.gateway.transport.floor_controller import FloorController
from vogon.gateway.transport.line_detector import LineDetector
from vogon_contract import (
    AbsorbedUtterance,
    DrainCleared,
    HeardHorizon,
    PlayoutFinished,
    UtteranceAbsorbed,
    UtteranceEager,
    UtteranceFinal,
    UtteranceResumed,
)

logger = logging.getLogger(__name__)

_TWILIO_STT_ENCODING = "mulaw"
_TWILIO_STT_SAMPLE_RATE = 8000
_STREAM_START_TIMEOUT = 10.0    # seconds to wait for Twilio "start" event

# Outbound TTS frame size: 160 bytes = 20 ms at 8 kHz mulaw (1 byte/sample).
# Aligns to Twilio's native packetisation and avoids fragmentation clicks.
_TWILIO_TTS_FRAME_BYTES = 160

# Observability thresholds. See docs/voice-timing-constants.md §5.
# A send-side frame gap only risks Twilio playback underrun when we are
# behind real-time (buffer debt > 0). When ratio > 1 Twilio already holds
# seconds of buffered audio, so even gaps of 200 ms are benign.  The warning
# is therefore gated on buffer debt — see _do_tts_stream inner loop.
_TTS_FRAME_GAP_WARN_MS = 60          # 3× the 20 ms frame period (gap threshold)
# A backed-up send queue only matters if frames can't reach Twilio in time.
# Normal burst delivery from Deepgram chunks briefly spikes depth to 10–30;
# only warn when the queue is deep enough to indicate a real ws drain stall.
_SEND_QUEUE_DEPTH_WARN = 25          # ~500 ms of audio backed up (raised from 5)

# Prefix for a re-speak after a mid-stream failure. The caller heard a fragment
# that cut off, and the apology is what makes the repetition sound intentional
# rather than a glitch (CLAUDE.md, TTS error recovery). It is never used on a
# clean retry: nothing reached the caller, so there is nothing to apologise for.
_RESPEAK_APOLOGY = "Sorry about that. "

# Legacy fallback constants — used only when tts_phone_encoding="linear16".
_TTS_SOURCE_RATE = 24000        # linear16 input rate from Deepgram TTS
_TTS_TARGET_RATE = 8000         # mulaw output rate for Twilio
_TTS_SAMPLE_WIDTH = 2           # bytes per linear16 sample

# Echo suppression: drop inbound audio while TTS is playing and for a short
# drain window after it ends, preventing our own voice from triggering STT.
_TTS_ECHO_COOLDOWN_SEC = 0.25

# Safety cap on the playback-drain window (mark sent, echo not yet received).
# The send loop runs ahead of real time, so Twilio keeps PLAYING for seconds
# after the stream finishes sending — barge-in must stay armed through that
# whole window. The cap only guards a lost mark echo from pinning the gate
# open forever; no legitimate reply plays longer than this.
_DRAIN_WINDOW_MAX_SEC = 90.0

# Barge-in detection is now settings-tunable (barge_in_rms_threshold /
# _consecutive_frames / _grace_ms), resolved per-instance in __init__.
# See docs/barge-in-tuning.md.


class TwilioTransport(BaseTransport):
    """Voice transport for Twilio Media Streams WebSocket calls."""

    def __init__(
        self,
        ws: WebSocket,
        send_raw: Callable[[str], Awaitable[None]],
        call_id: str = "",
        call_config=None,   # gateway CallConfig — the ONLY config input
        start_event: dict | None = None,
        qsize_fn: Callable[[], int] | None = None,
        recorder: AudioRecorder | None = None,
        audio_event_sink=None,
        line_instrument=None,   # ask(state) -> P(recording); installed by the driver
    ) -> None:
        self._ws = ws
        self._send_raw = send_raw          # puts raw JSON str on the priority send queue
        self._call_id = call_id
        # Barge policy (M1 item 9): the APPLICATION supplies the break-through
        # vocabulary and the judge switch via CallConfig.barge_policy; the
        # gateway ships no domain phrases of its own.
        _bp = getattr(call_config, "barge_policy", None) if call_config is not None else None
        self._safety_phrases: tuple = tuple(_bp.safety_phrases) if _bp else ()
        self._floor_phrases: tuple = tuple(_bp.floor_request_phrases) if _bp else ()
        self._semantic_judge: bool = _bp.semantic_judge if _bp else True
        self._cfg = call_config
        self._pre_read_start = start_event  # start event already consumed by websocket_twilio
        self._recorder = recorder
        self._aec: AECProcessor | None = create_aec()
        # Created BEFORE the clock engine — clocks emits into it (silence/
        # duration firings, the filler hold) so the audio timeline can show
        # the ladder decisions, not just their audible consequences.
        self._audio_log = AudioEventLog(audio_event_sink)
        self._clocks: CallClocks | None = (
            CallClocks(call_config, self._enqueue_event, self.send_response,
                       audio_emit=self._audio_log.emit)
            if call_config is not None else None
        )

        self._stream_sid: str | None = None
        self._stt_open_ms: float | None = None
        self._utterance_queue: asyncio.Queue = asyncio.Queue()
        # Phone-optimised TTS: request mulaw 8kHz directly from Deepgram so no
        # Python-side resampling is needed. container="none" suppresses the WAV
        # wrapper, giving us raw mulaw bytes with no header to strip.
        _phone_enc = settings.tts_phone_encoding
        _phone_rate = settings.tts_phone_sample_rate
        self._tts = TTSClient(
            voice=(call_config.persona_voice or None)
            if call_config is not None else None,
            encoding=_phone_enc,
            sample_rate=_phone_rate,
            container="none" if _phone_enc == "mulaw" else None,
        )
        self._tts_encoding = _phone_enc
        self._qsize_fn = qsize_fn   # callable → int; None when not wired
        # Pending mark names → (monotonic send timestamp, text snippet).
        # Used to confirm Twilio played through the audio (mark echo round-trip).
        self._pending_marks: dict[str, tuple[float, str]] = {}
        self._tts_playing = False
        # Set when a turn ends with no audio delivered; cleared as
        # soon as a later turn is heard. None = the caller can hear us.
        self._tts_mute_since: float | None = None
        self._tts_cancel = asyncio.Event()
        self._tts_ended_at: float = 0.0   # monotonic timestamp TTS last finished
        # Delivery reporting (docs/speech-delivery-completion.md): the audio_id
        # of the last send_response, and the id whose buffered tail a drain-path
        # barge cleared AFTER send_response returned (late truncation).
        self._last_audio_id: str = ""
        # First-chunk timing for the current playout (metrics inversion —
        # reported on the DeliveryReport; handler owns emission).
        self._fc_ms: float | None = None
        self._fc_t: float | None = None
        # Full-duplex semantic barge-in (docs/full-duplex-semantic-barge-in.md).
        self._last_speak_text: str = ""            # text of the current/last playout
        self._last_interim_text: str = ""          # caller interim during the window
        self._yield_task: asyncio.Task | None = None
        self._absorbing_utterance: bool = False    # absorb decided; awaiting the final EOT
        self._absorb_detail: dict | None = None
        # docs/full-duplex-turn-model.md §C — absorb <-> silence coupling:
        # an absorbed utterance is SPEECH, not silence. Absorbed activity is
        # reported to the gateway clocks (note_caller_activity) so the silence
        # ladder never fires an "are you still there?" probe at a caller we
        # just chose to ignore.
        self._last_caller_activity_t: float = 0.0
        # Carry-forward buffer: the swallowed final transcript, kept for one
        # utterance / ~20s so the NEXT real utterance's interpretation can see
        # both noisy samples of what may be the same statement.
        self._recent_absorbed: dict | None = None
        # Heard-horizon anchoring (docs/full-duplex-turn-model.md §A): a compact
        # ledger of playout attempts — {audio_id, text, start_t, end_t|None} —
        # start stamped at send_response entry, end at the tts_end_ mark echo
        # (or the fallback sites that stamp _tts_ended_at). _compute_horizon()
        # reads it to answer "what had PLAYED when the caller started speaking".
        self._delivery_ledger: list[dict] = []
        # §Phase 3: running estimate of the outbound observation latency (the
        # jitter-buffer lead measured at each mark echo), for projecting a
        # playout-complete time onto the media frame in duration queries.
        self._outbound_offset = OffsetEstimator()
        # Monotonic time of the FIRST STT interim of the current utterance —
        # the caller-speech-onset proxy carried on the utterance events' onset_at.
        self._utterance_onset_t: float = 0.0
        self._candidate_raw_rms: int = 0           # rms at energy-confirm (for cut telemetry)
        self._candidate_clean_rms: int = 0
        # Bounded playout buffer (Phase 3, full-duplex-semantic-barge-in §6).
        self._graceful_yield: bool = False         # answer_to_heard: stop after current sentence
        self._current_sentences: list[str] = []    # sentences of the active playout
        self._sentences_played: int = 0            # per-sentence mark echoes received
        # Intro/farewell protection: the active playout must play to completion
        # (barge never cuts it; safety phrases excepted). Lives for the playout
        # lifetime — replaced on the next send_response.
        self._protected_playout: bool = False
        self._absorb_playout: bool = False
        self._last_utterance_t: float = 0.0
        self._agent_done_t: float = 0.0
        self._response_agent_id: str = ""
        self._stt: STTSession | None = None
        self._barge_in_attempts: int = 0
        self._barge_in_in_progress: bool = False
        # Legacy fixed threshold — used only by the no-AEC fallback path and
        # the SNR signal/noise classification below.
        self._barge_rms_threshold: int = settings.barge_in_rms_threshold
        self._barge_grace_sec: float = settings.barge_in_grace_ms / 1000.0
        # Tier-2 decision core (docs/barge-in-tuning.md): pure FSM — energy
        # pre-gate + optional adaptive threshold (Phase A) + optional local-VAD
        # confirm (Phase B1). At default settings (adaptive off, confirm off)
        # it reproduces the Tier-1 inline behaviour exactly.
        self._barge_gate = BargeGate(
            BargeGateConfig(
                rms_threshold=settings.barge_in_rms_threshold,
                consecutive_frames=max(1, settings.barge_in_consecutive_frames),
                grace_sec=settings.barge_in_grace_ms / 1000.0,
                adaptive_enabled=settings.barge_in_adaptive_enabled,
                adaptive_alpha=settings.barge_in_adaptive_alpha,
                adaptive_margin=settings.barge_in_adaptive_margin,
                adaptive_min=settings.barge_in_adaptive_min,
                adaptive_max=settings.barge_in_adaptive_max,
                adaptive_warmup_frames=settings.barge_in_adaptive_warmup_frames,
                confirm=settings.barge_in_confirm,
                confirm_frames_m=settings.barge_in_confirm_frames_m,
                confirm_frames_n=settings.barge_in_confirm_frames_n,
                confirm_timeout_sec=settings.barge_in_confirm_timeout_ms / 1000.0,
                confirm_energy_floor_frames=settings.barge_in_confirm_energy_floor_frames,
            ),
            confirm_frame=make_confirm_frame(
                settings.barge_in_confirm, settings.barge_in_vad_aggressiveness,
            ),
        )
        self._tts_started_at: float = 0.0          # monotonic; for the start-grace
        # Phase 0 measurement (docs/barge-in-tuning.md): per-frame rows + the
        # post-AEC mulaw stream a VAD confirm step would see. None = disabled
        # (the default) → zero hot-path cost beyond one `is not None` check.
        self._frame_log: list[tuple] | None = [] if settings.barge_in_frame_log else None
        self._frame_log_chunks: list[bytes] = []
        self._frame_log_t0: float = 0.0
        # (the sparse audio-event stream — self._audio_log — is created
        # earlier, before the clock engine that also emits into it)
        self._stt_interim_seen = False  # emit only the FIRST interim per turn
        # SNR accumulators: signal = caller speech (non-gated, above threshold);
        # noise = quiet frames (gated below threshold, or silence between words).
        self._signal_rms_sum: float = 0.0
        self._signal_frame_count: int = 0
        self._noise_rms_sum: float = 0.0
        self._noise_frame_count: int = 0
        # Recording: per-utterance byte cursor so that back-to-back frames
        # (sent in microseconds by the TTS flush loop) land at the correct
        # sample offsets rather than all piling up at the same timestamp.
        self._rec_tts_start_ts: float = 0.0
        self._rec_tts_byte_offset: int = 0

        # ── Ambience (liveliness) — server-side, this is the dumb-client path ──
        # Unlike the widget (which mixes itself in the browser), Twilio has no
        # smart client: the server must put every byte on the wire. So the
        # Phase-1 AmbienceMixer runs here, mixing room tone (ducked) into TTS
        # frames and a paced task fills the between-turn gap with room tone +
        # typing. See docs/ambience-liveliness.md.
        _amb = (call_config.ambience or {}) if call_config is not None else {}
        self._amb_enabled = bool(_amb.get("enabled", settings.ambience_enabled))
        self._amb_typing = bool(_amb.get("typing", settings.ambience_typing))
        self._amb_oneshots = bool(_amb.get("oneshots", settings.ambience_oneshots))
        self._amb_typing_delay = float(
            _amb.get("typing_delay_ms", settings.ambience_typing_delay_ms)
        ) / 1000.0
        self._amb_oneshot_min_gap = float(
            _amb.get("oneshot_min_gap_sec", settings.ambience_oneshot_min_gap_sec)
        )
        self._ambience = None
        if self._amb_enabled:
            self._ambience = build_mixer(
                _TWILIO_STT_SAMPLE_RATE,  # 8 kHz — the phone rate
                enable_room=bool(_amb.get("room", settings.ambience_room_tone)),
                enable_typing=self._amb_typing,
                enable_oneshots=self._amb_oneshots,
                config=AmbienceConfig(
                    room_gain=float(_amb.get("room_gain", settings.ambience_room_gain)),
                    duck_factor=float(_amb.get("duck_factor", settings.ambience_duck_factor)),
                    typing_gain=float(_amb.get("typing_gain", settings.ambience_typing_gain)),
                ),
            )
        self._ambience_task: asyncio.Task | None = None
        self._amb_stop = asyncio.Event()
        # Ambience is emitted ONLY during an active think-gap (STT-final → TTS
        # start), never continuously — continuous emission pins Twilio's playout
        # buffer at the TTS burst-lead depth and adds compounding latency.
        self._gap_active: bool = False
        self._typing_arm_deadline: float = 0.0
        self._typing_idx: int = 0
        self._oneshot_idx: int = 0
        self._last_oneshot_t: float = 0.0
        self._audio_route = None
        self._playback_lock = asyncio.Lock()
        self._floor = (FloorController(self, lambda: time.monotonic())
                       if settings.barge_controller_enabled and settings.semantic_barge_enabled else None)
        # Who answered the line (line_detector.py): reads the floor
        # controller's post-AEC episodes, so it needs the controller.
        _ld = getattr(self._cfg, "line_detection", None) if self._cfg is not None else None
        self._line = (LineDetector(self, _ld, **({"ask": line_instrument} if line_instrument else {}))
                      if _ld is not None and _ld.enabled and self._floor is not None else None)

    # ── BaseTransport interface ───────────────────────────────────────────────

    @property
    def utterance_queue(self) -> asyncio.Queue:
        return self._utterance_queue

    def _enqueue_event(self, event) -> None:
        """Clock events (SilenceThreshold/DurationThreshold) ride the same
        FIFO queue as utterances — one total order of transport facts."""
        self._utterance_queue.put_nowait(event)

    def _note_activity(self) -> None:
        """Caller made themselves heard (any utterance event, incl. absorbed
        barges — §C: absorbed speech is still speech). The only signal that
        re-arms the silence window."""
        if self._clocks is not None:
            self._clocks.note_caller_activity()

    @staticmethod
    def _is_turn_audio(audio_id: str) -> bool:
        """True for a real agent turn; False for the silence ladder's own
        speech. Only a real turn restarts the silence window — resetting it on
        a probe would mean the exit threshold never arrives and a call the
        caller has walked away from is never hung up."""
        return not str(audio_id or "").startswith(("silence-probe", "silence-exit"))

    def _note_playout_end(self, reset_window: bool = False) -> None:
        """Agent audio finished actually playing (mark echo / fallback /
        drain-clear). Un-pauses the silence clock; never resets the window."""
        if self._clocks is not None:
            self._clocks.note_playout_end(reset_window=reset_window)
        if self._line is not None:
            self._line.note_playout_end()

    async def wait_playout_heard(self, cap_sec: float) -> bool:
        """Wait for Twilio to echo the marks of audio already sent — the
        point at which the caller has HEARD it. A call update (the warm
        transfer's hold redirect) drops whatever Twilio still buffers, so
        the handoff line waits here first. Bounded by cap_sec for a mark
        that never echoes; False = the cap ran out."""
        deadline = time.monotonic() + cap_sec
        while self._playback_draining:
            if time.monotonic() >= deadline:
                logger.warning("playout not confirmed heard within %.1fs — "
                               "proceeding", cap_sec)
                return False
            await asyncio.sleep(0.05)
        return True

    async def wait_line_quiet(self) -> None:
        """Hold a `play_after="line_quiet"` playout until the far end has
        stopped talking (line_detector.wait_quiet). No detector = no hold."""
        if self._line is not None:
            await self._line.wait_quiet()

    @property
    def _playback_draining(self) -> bool:
        """True while Twilio is still PLAYING audio we already finished sending.

        The send loop runs ahead of real time, so the stream ends
        (_tts_playing False) seconds before playback does. A mark is sent at
        stream end and echoed by Twilio only after playback completes —
        pending marks are the exact drain signal. Without this, barge-in
        detection went dark for the entire buffered tail (the caller talked,
        the agent kept playing). Age-capped so a lost echo cannot pin the
        gate open forever.
        """
        if not self._pending_marks:
            return False
        oldest_sent_t = min(t for t, _ in self._pending_marks.values())
        return (time.monotonic() - oldest_sent_t) < _DRAIN_WINDOW_MAX_SEC

    def _close_ledger(self, end_t: float) -> None:
        """Stamp end_t on the most recent still-open playout ledger entry and
        report it as a PlayoutFinished event — the push replacement for the
        playback_complete_t() poll (phase0 §4.2, step 4). All completion
        moments funnel here: mark echo, no-mark fallback, drain-clear."""
        for entry in reversed(self._delivery_ledger):
            if entry["end_t"] is None:
                entry["end_t"] = end_t
                self._enqueue_event(
                    PlayoutFinished(audio_id=entry["audio_id"], at=end_t)
                )
                break

    def _refine_onset_from_words(self) -> None:
        """§Phase 3: upgrade the caller-speech-onset from the first-interim
        ARRIVAL proxy to the true onset (Deepgram word .start, anchored on our
        monotonic clock) when word timings are available. The interim proxy lags
        real onset by the STT delivery latency; the word start does not. No-op
        when the STT session has no word timing for this utterance."""
        if self._stt is None:
            return
        corrected = self._stt.last_onset_monotonic()
        if corrected is not None:
            self._utterance_onset_t = corrected

    def outbound_offset_s(self) -> float:
        """§Phase 3: current smoothed outbound observation latency (jitter lead)
        for projecting a still-in-flight speak's playout-complete onto the media
        frame in duration queries."""
        return self._outbound_offset.value()

    def _compute_horizon(self, onset_t: float) -> HeardHorizon | None:
        """What had PLAYED when the caller started speaking (§A) — computed at
        event-enqueue time and attached to the utterance event (phase0 §4.2,
        replacing the heard_horizon() poll).

        docs/full-duplex-turn-model.md — the interpretation anchor for
        full-duplex turns: the caller answers the question at their EAR, not
        the latest emitted text. Reads the delivery ledger:

        - a playout with end_t <= onset was fully heard (→ `completed`);
        - a playout with start_t <= onset < end_t (or still open) was heard up
          to a word-proportional prefix of (onset - start_t) (→ `in_flight`);
        - a playout with start_t > onset was NOT heard at all when the caller
          began — it is the emission the caller is NOT answering.

        Returns None when there is nothing to anchor on (no onset, empty
        ledger).
        """
        if not onset_t or not self._delivery_ledger:
            return None

        completed: list[tuple[float, str]] = []   # (end_t, audio_id) for ordering
        last_heard: str = ""
        unheard: str = ""
        in_flight_id: str | None = None
        in_flight_prefix: str = ""
        for entry in self._delivery_ledger:
            if entry["start_t"] > onset_t:
                # Emitted after the caller began — entirely unheard at onset.
                unheard = (unheard + " " + entry["text"]).strip()
                continue
            end_t = entry["end_t"]
            if end_t is not None and end_t <= onset_t:
                completed.append((end_t, entry["audio_id"]))
                last_heard = entry["text"]
                continue
            # In flight at onset: heard a proportional prefix.
            in_flight_id = entry["audio_id"]
            est_total = estimate_total_ms(entry["text"])
            heard_ms = max(0.0, (onset_t - entry["start_t"]) * 1000.0)
            prefix = estimate_heard_text(entry["text"], heard_ms, est_total)
            in_flight_prefix = prefix
            if prefix:
                last_heard = prefix
            remainder = entry["text"][len(prefix):].strip() if prefix else entry["text"]
            unheard = (remainder + " " + unheard).strip()
        completed.sort(key=lambda pair: pair[0])
        return HeardHorizon(
            completed=[aid for _, aid in completed],
            in_flight=in_flight_id,
            in_flight_heard_text=in_flight_prefix,
            last_heard_text=last_heard,
            unheard_text=unheard,
        )

    def _take_carry_forward(self, *, clear: bool, ttl_s: float = 20.0):
        """Carry-forward of the most recently ABSORBED utterance
        (docs/full-duplex-turn-model.md §C), attached at enqueue time to BOTH
        the eager and the final of the next turn (phase0 §4.2 — no destructive
        read, no eager/final race; replaces consume_recent_absorbed). The
        record clears when a final is enqueued (clear=True). The absorbed text
        rides the turn's structured annotations so classifiers see both noisy
        samples of what is often the same statement. Entries older than
        ``ttl_s`` are dropped — stale absorbed text is noise, not context.
        Returns gateway.contract.AbsorbedUtterance or None.
        """
        buf = getattr(self, "_recent_absorbed", None)
        if clear:
            self._recent_absorbed = None
        if not buf:
            return None
        if time.monotonic() - buf["t"] > ttl_s:
            self._recent_absorbed = None
            return None
        return AbsorbedUtterance(
            text=buf["text"], verdict=buf.get("verdict", ""), at=buf["t"],
        )

    async def start(self) -> None:
        """Open STT session then wait for Twilio's 'start' event to get streamSid."""
        if self._clocks is not None:
            self._clocks.start()
        if self._floor is not None:
            self._floor.start()
        if self._line is not None:
            self._line.start()
        async def on_interim(text: str, confidence: float) -> None:
            # NOTE (post-AEC): interims DO fire during TTS/drain — the BargeGate
            # forwards cleaned frames to STT during barge candidacy. The semantic
            # yield-pending window reads the latest interim from here.
            self._last_interim_text = text
            if self._floor is not None:
                _ep = await self._floor.transcript(text)
                if self._line is not None:
                    await self._line.observe(_ep, final=False)
            # The caller is audibly talking RIGHT NOW — re-arm the silence
            # window. Flux sends a final only at EndOfTurn (pipeline/stt.py §
            # "Turn-end detection"), so without this the window accumulates
            # straight through an uninterrupted answer and the ladder fires
            # over live speech: measured on the clock engine, 9 s of
            # continuous talking earns "Are you still there?" spoken over the
            # caller. Re-arming on the FIRST interim only would not fix it —
            # accumulation is continuous, so a single reset at the start of a
            # 15 s answer still crosses warn at second 8. It has to track the
            # speech, which is what the per-Update interims give us.
            #
            # Gated on not-playing, and that guard is not optional: interims
            # also fire during our OWN audio (see the note above — barge
            # candidacy forwards AEC-cleaned frames to STT). Re-arming there
            # buys nothing, because playback already pauses the clock, and it
            # would let an echo zero the window and clear the fired set on a
            # call nobody is on — making the exit threshold unreachable and
            # running the call to the max-duration watchdog. That is the
            # ratified pause-don't-reset rule (clocks.py header), pinned by
            # test_playback_pauses_idle_clock_without_reset.
            #
            # Finals and eager-ends stay unconditional: a final is PROOF the
            # caller spoke, an interim is only evidence, and evidence arriving
            # over our own voice is the ambiguous kind.
            if not (self._tts_playing or self._playback_draining):
                self._note_activity()
            # First interim per turn = the caller-speech-start proxy marker.
            if not self._stt_interim_seen:
                self._stt_interim_seen = True
                # §A: first interim per turn = caller-speech-onset proxy.
                self._utterance_onset_t = time.monotonic()
                self._audio_log.emit(
                    time.monotonic(), "stt_interim", text=text[:60], confidence=confidence,
                )

        async def on_eager_end(
            text: str, confidence: float, elapsed_ms: float, low_conf_words: list[str]
        ) -> None:
            # Start the response-latency clock here so that the eager speculative
            # pipeline is included in the measurement.  on_final (below) must not
            # reset it for the same turn.
            self._last_utterance_t = time.monotonic()
            self._refine_onset_from_words()
            self._audio_log.emit(self._last_utterance_t, "eager_eot", text=text[:60])
            if self._absorbing_utterance:
                # Absorbed barge: suppress speculation for this utterance — the
                # handler skips absorbed eagers; the final decides swallowing.
                self._note_activity()
                _detail = self._absorb_detail or {}
                await self._utterance_queue.put(UtteranceAbsorbed(
                    text=text, phase="eager",
                    verdict=_detail.get("verdict", ""),
                    source=_detail.get("source", ""),
                    window_ms=float(_detail.get("window_ms", 0) or 0),
                    audio_id=_detail.get("audio_id", "") or "",
                    onset_at=self._utterance_onset_t, at=self._last_utterance_t,
                    seq=self.next_event_seq(),
                ))
                return
            self._arm_typing()
            self._note_activity()
            await self._utterance_queue.put(
                UtteranceEager(text=text, confidence=confidence,
                               playback_generation=self.playback_generation(),
                               low_conf_words=low_conf_words,
                               onset_at=self._utterance_onset_t,
                               at=self._last_utterance_t,
                               heard=self._compute_horizon(self._utterance_onset_t),
                               carry_forward=self._take_carry_forward(clear=False),
                               seq=self.next_event_seq())
            )

        async def on_turn_resumed() -> None:
            if self._floor is not None:
                self._floor.resume()
            self._note_activity()
            await self._utterance_queue.put(
                UtteranceResumed(at=time.monotonic(), seq=self.next_event_seq())
            )

        async def on_final(text: str, confidence: float, elapsed_ms: float, low_conf_words: list[str]) -> None:
            final_generation = self.playback_generation()
            # Only set the anchor if on_eager_end did not already start the clock
            # for this turn.  For the non-eager path this is always 0.0 here.
            if not self._last_utterance_t:
                self._last_utterance_t = time.monotonic()
            self._refine_onset_from_words()
            self._stt_interim_seen = False  # next interim marks a new turn
            self._audio_log.emit(
                time.monotonic(), "stt_final", text=text[:60], confidence=confidence,
            )
            self._last_interim_text = ""  # utterance over — clear the window's view
            if self._floor is not None:
                episode = await self._floor.transcript(text, final=True)
                if self._line is not None:
                    await self._line.observe(episode, final=True)
                if episode is self._floor.current and episode.final:
                    final_generation = self.playback_generation()
                if self._floor.caller_floor or episode.deferred:
                    self._absorbing_utterance = False
                    self._absorb_detail = None
                elif episode.decision == 'UNCERTAIN' and self._floor.active_playback:
                    self._absorbing_utterance = True
                    self._absorb_detail = dict(text=text, verdict='timeout_absorb',
                                              source='floor_controller', audio_id=self._last_audio_id)

            # ── Open STT feed: judge finals that arrive DURING our playback ──
            # With the feed open, Deepgram now transcribes caller speech that
            # never tripped the energy gate (quiet talkers) — and possibly our
            # own leaked voice. Such a final must not silently run a turn while
            # our audio keeps playing (the agent would answer itself). If the
            # yield window is still deciding, wait for it; then, when no cut or
            # absorb has happened yet, judge the final through the same verdict
            # machinery (echo guard → heuristic → capped Cerebras).
            # docs/stt-open-feed.md.
            if (
                settings.stt_feed_mode == "open"
                and self._floor is None
                and (self._tts_playing or self._playback_draining)
            ):
                if self._yield_task is not None and not self._yield_task.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self._yield_task),
                            timeout=(settings.semantic_barge_window_ms + 300) / 1000.0,
                        )
                    except Exception:  # noqa: BLE001 — window outcome checked below
                        pass
                if (
                    not self._tts_cancel.is_set()
                    and not self._graceful_yield
                    and not self._absorbing_utterance
                ):
                    await self._judge_playback_final(text)

            if self._absorbing_utterance:
                # Absorb decided mid-utterance. If the FINAL is still a
                # backchannel/noise, swallow the turn (tagged event → handler
                # logs BARGE_ABSORBED and skips). If the caller kept talking
                # into real content, process it normally — the absorb only
                # spared the audio.
                detail = {**(self._absorb_detail or {}), "final_text": text}
                # An echo verdict ALWAYS swallows — echo fragments of our own
                # speech are long, so the grew-substantive re-check below would
                # wrongly promote them to a real turn.
                swallow = should_swallow_absorbed_final(
                    detail.get("verdict", ""), text,
                    backchannel_max_tokens=settings.semantic_barge_backchannel_max_tokens,
                    substantive_min_tokens=settings.semantic_barge_substantive_min_tokens,
                    safety_phrases=self._safety_phrases,
                    floor_phrases=self._floor_phrases,
                    playout_text=self._last_speak_text,
                )
                self._absorbing_utterance = False
                self._absorb_detail = None
                if swallow:
                    if self._floor is not None:
                        self._floor.emit('caller_transcript', detail.get('verdict', 'continue'),
                                         action='SUPPRESS_TURN', text=text)
                    else:
                        self._audio_log.emit(time.monotonic(), 'caller_transcript',
                                             reason=detail.get('verdict', 'continue'),
                                             audio_id=detail.get('audio_id'),
                                             action='SUPPRESS_TURN', text=text)
                    # §C: absorbed speech is still speech — feed the activity
                    # clock and keep the text for the next real utterance.
                    now = time.monotonic()
                    self._last_caller_activity_t = now
                    self._note_activity()
                    # The previous carry-forward record is cleared here exactly
                    # as the untagged final does (a swallowed final replaces,
                    # not consumes, its own record below); the absorbed event
                    # itself never carries it — the backend logs and skips.
                    self._take_carry_forward(clear=True)
                    await self._utterance_queue.put(UtteranceAbsorbed(
                        text=detail.get("text", "") or text, phase="final",
                        verdict=detail.get("verdict", ""),
                        source=detail.get("source", ""),
                        window_ms=float(detail.get("window_ms", 0) or 0),
                        audio_id=detail.get("audio_id", "") or "",
                        final_text=text,
                        onset_at=self._utterance_onset_t, at=self._last_utterance_t,
                        seq=self.next_event_seq(),
                    ))
                    # THIS absorbed utterance becomes the carry-forward for the
                    # next real turn (set after the take above so a swallowed
                    # final replaces, not consumes, its own record).
                    self._recent_absorbed = {
                        "text": text, "t": now,
                        "verdict": detail.get("verdict", ""),
                    }
                    return
                logger.info(
                    "Semantic barge: absorbed utterance grew substantive — processing normally | text=%r",
                    text[:80],
                )
            self._arm_typing()
            self._note_activity()
            if self._floor is not None:
                self._floor.accepted_generation = final_generation
                self._floor.emit('caller_transcript', 'caller_turn_allowed', action='ALLOW_TURN', text=text)
            await self._utterance_queue.put(
                UtteranceFinal(text=text, confidence=confidence,
                               playback_generation=final_generation,
                               low_conf_words=low_conf_words,
                               onset_at=self._utterance_onset_t,
                               at=self._last_utterance_t,
                               heard=self._compute_horizon(self._utterance_onset_t),
                               carry_forward=self._take_carry_forward(clear=True),
                               seq=self.next_event_seq())
            )

        name_hint = self._cfg.name_hint if self._cfg is not None else None
        keyterms = name_hint.split() if name_hint else []

        self._stt = self._make_stt(
            on_interim=on_interim,
            on_final=on_final,
            language=self._cfg.language if self._cfg is not None else "en-US",
            keyterms=keyterms,
            encoding=_TWILIO_STT_ENCODING,
            sample_rate=_TWILIO_STT_SAMPLE_RATE,
            eot_timeout_ms=settings.deepgram_phone_eot_timeout_ms,
            eot_threshold=settings.deepgram_phone_eot_threshold,
            eager_eot_threshold=settings.deepgram_phone_eager_eot_threshold,
            on_eager_end=on_eager_end,
            on_turn_resumed=on_turn_resumed,
        )
        # Reported as data in transport_analytics(); the transport writes
        # no analytics rows itself.
        self._stt_open_ms = await self._stt.start()

        await self._tts.open()

        # If the websocket_twilio handler already consumed the start event
        # (to authenticate via callSid), use it directly instead of re-reading.
        if self._pre_read_start:
            self._stream_sid = (
                self._pre_read_start.get("streamSid")
                or self._pre_read_start.get("start", {}).get("streamSid")
            )
            call_sid = self._pre_read_start.get("start", {}).get("callSid", "unknown")
            logger.info(
                "Twilio stream started (pre-read) | stream_sid=%s | call_sid=%s",
                self._stream_sid, call_sid,
            )
        else:
            await self._wait_for_stream_start()

        # Start the between-turn ambience playout now that streamSid is known.
        if self._ambience is not None and self._stream_sid:
            self._ambience_task = asyncio.create_task(self._ambience_loop())

    def _make_stt(self, **kw) -> STTSession:
        """The listener. The one seam a parallel transport overrides:
        LiveTransport returns a gpt-live-1 session with the same callbacks
        (docs/GPT_LIVE_TURN_API_DESIGN.md §5)."""
        return STTSession(**kw)

    async def _wait_for_stream_start(self) -> None:
        logger.debug("Twilio: waiting for 'start' event to get streamSid")
        try:
            deadline = asyncio.get_event_loop().time() + _STREAM_START_TIMEOUT
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                message = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
                if "text" not in message or not message["text"]:
                    continue
                msg = json.loads(message["text"])
                event = msg.get("event")
                if event == "connected":
                    logger.debug("Twilio: connected event received")
                elif event == "start":
                    # The stream epoch — the recording's timeline origin, so
                    # it starts when the call connects rather than when the
                    # first sound happens to be captured. Not the frame
                    # placement anchor; see AudioRecorder.set_stream_epoch.
                    if self._recorder is not None:
                        self._recorder.set_stream_epoch(time.monotonic())
                    # streamSid is at the top level in newer protocol versions
                    self._stream_sid = (
                        msg.get("streamSid")
                        or msg.get("start", {}).get("streamSid")
                    )
                    call_sid = msg.get("start", {}).get("callSid", "unknown")
                    logger.info(
                        "Twilio stream started | stream_sid=%s | call_sid=%s",
                        self._stream_sid, call_sid,
                    )
                    return
        except asyncio.TimeoutError:
            logger.warning("Twilio: timed out waiting for 'start' event")
        except Exception as exc:
            logger.error("Twilio: error waiting for start event: %s", exc)

    async def close(self) -> None:
        if self._floor is not None:
            await self._floor.close()
        if self._line is not None:
            await self._line.close()
        if self._clocks is not None:
            await self._clocks.stop()
        try:
            self._flush_frame_log()
        except Exception as exc:  # noqa: BLE001 — diagnostics must never block teardown
            logger.warning("barge frame log flush failed: %s", exc)
        try:
            self._audio_log.flush(
                Path(settings.recordings_path) / f"{self._call_id}.audio_events.json",
                t0=self._recorder.t0 if self._recorder is not None else None,
                meta={
                    "call_id": self._call_id,
                    "barge_in_rms_threshold": self._barge_gate.cfg.rms_threshold,
                    "barge_in_consecutive_frames": self._barge_gate.cfg.consecutive_frames,
                    "barge_in_grace_ms": int(self._barge_gate.cfg.grace_sec * 1000),
                    "barge_in_adaptive": self._barge_gate.cfg.adaptive_enabled,
                    "barge_in_confirm": self._barge_gate.cfg.confirm,
                    "barge_controller_enabled": self._floor is not None,
                    "ambience_enabled": self._amb_enabled,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("audio event log flush failed: %s", exc)
        self._amb_stop.set()
        if self._ambience_task is not None:
            self._ambience_task.cancel()
            try:
                await self._ambience_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._stt:
            await self._stt.close()
        await self._tts.close()

    async def handle_binary_frame(self, data: bytes) -> None:
        pass  # Twilio never sends binary frames

    async def handle_text_frame(self, msg: dict) -> bool:
        """Process a Twilio Media Streams event. Returns True on call stop."""
        event = msg.get("event")

        if event == "media":
            media = msg.get("media", {})
            payload = media.get("payload")
            if payload and self._stt:
                now = time.monotonic()
                mulaw_bytes = base64.b64decode(payload)
                if self._recorder is not None:
                    # Position by Twilio's STREAM clock, not arrival time —
                    # jitter was displacing audio inside the recording by up
                    # to 17 ms of a 20 ms frame. See AudioRecorder
                    # .caller_timestamp for the measurement.
                    self._recorder.add_caller_chunk(
                        self._recorder.caller_timestamp(media, now), mulaw_bytes,
                    )
                # Decode once — used for barge-in detection and SNR tracking.
                try:
                    pcm = audioop.ulaw2lin(mulaw_bytes, 2)
                    rms = audioop.rms(pcm, 2)
                except Exception:
                    rms = 0
                active_audio = (self._tts_playing or self._playback_draining or
                                now-self._tts_ended_at < _TTS_ECHO_COOLDOWN_SEC)
                route = ('aec_open' if settings.stt_feed_mode == 'open' else 'aec_gated') if active_audio and self._aec is not None else 'suppressed_no_aec' if active_audio else 'raw_open'
                if route != self._audio_route:
                    self._audio_route = route
                    self._audio_log.emit(now, 'audio_route', reason=route,
                                         stt_feed_mode=settings.stt_feed_mode,
                                         aec_available=self._aec is not None)
                # Barge window = send window + PLAYBACK-DRAIN window + echo
                # cooldown. The drain term is load-bearing: sending runs ahead
                # of real time, so without it detection went dark the moment
                # the stream finished sending while Twilio played the buffered
                # remainder — a barge mid-reply left the audio running.
                if (
                    self._tts_playing
                    or self._playback_draining
                    or (now - self._tts_ended_at) < _TTS_ECHO_COOLDOWN_SEC
                ):
                    if self._aec is not None:
                        # AEC path: cancel echo; the gate decides barge ACTIONS.
                        clean_mulaw = self._aec.process(mulaw_bytes)
                        try:
                            clean_pcm = audioop.ulaw2lin(clean_mulaw, 2)
                            clean_rms = audioop.rms(clean_pcm, 2)
                        except Exception:
                            clean_rms = 0
                        # ── Open STT feed (docs/stt-open-feed.md) ─────────────
                        # Send the AEC-cleaned frame to Deepgram UNCONDITIONALLY
                        # so the caller's full utterance is always heard. The
                        # old valve (frames reach STT only via a confirmed
                        # barge) clipped quiet/overlapping speech during agent
                        # playback and fragmented the caller's turn. The gate
                        # below still runs on the same frame — but only to
                        # decide whether to cut playback. Echo protection moves
                        # from the audio layer (dropping frames) to the text
                        # layer (the echo-transcript guard in on_final).
                        if settings.stt_feed_mode == "open":
                            await self._stt.send_audio(clean_mulaw)
                        # ── BargeGate decision (Tier 1 + Phase A/B1) ──────────
                        # The pure FSM decides; this transport performs the
                        # I/O it prescribes. See app/transport/barge_gate.py.
                        verdict = self._barge_gate.on_frame(
                            now, clean_rms, self._tts_playing, clean_mulaw
                        )
                        for _ev, _detail in verdict.events:
                            self._audio_log.emit(now, _ev, **_detail)
                        if verdict.confirmed_now:
                            self._barge_in_attempts += 1
                            if self._floor is not None:
                                self._candidate_raw_rms = rms
                                self._candidate_clean_rms = clean_rms
                            elif settings.semantic_barge_enabled:
                                # Full-duplex semantic barge-in (Phase 1): the
                                # energy confirm is a CANDIDATE, not a verdict.
                                # Open the yield-pending window — audio keeps
                                # playing while the heuristic floor + Cerebras
                                # verdict decide absorb vs cut. Re-confirms
                                # while a window is open (or while absorbing
                                # the current utterance) are ignored.
                                if (
                                    not self._absorbing_utterance
                                    and (self._yield_task is None or self._yield_task.done())
                                ):
                                    self._candidate_raw_rms = rms
                                    self._candidate_clean_rms = clean_rms
                                    self._audio_log.emit(
                                        now, "barge_candidate", clean_rms=clean_rms,
                                        protected=self._protected_playout,
                                    )
                                    self._yield_task = asyncio.create_task(
                                        self._run_protected_window()
                                        if self._protected_playout
                                        else self._run_yield_window()
                                    )
                            else:
                                self._emit_barge_cut_telemetry(rms, clean_rms)
                                await self._cut_playback()
                        if verdict.noise_frame:
                            self._noise_rms_sum += clean_rms
                            self._noise_frame_count += 1
                        if self._floor is not None:
                            await self._floor.frame(clean_rms, self._barge_gate.threshold,
                                                    len(clean_mulaw)/8, verdict.confirmed_now)
                        if settings.stt_feed_mode != "open":
                            # gated (kill switch): the old valve — STT hears
                            # only confirmed-barge audio during agent playback.
                            for _f in verdict.forward_frames:
                                await self._stt.send_audio(_f)
                        if self._frame_log is not None:
                            self._log_frame(now, rms, clean_rms, clean_mulaw)
                    else:
                        # No AEC: legacy full suppression gate.
                        if rms > self._barge_rms_threshold:
                            if not self._barge_in_in_progress:
                                self._barge_in_attempts += 1
                                self._barge_in_in_progress = True
                        else:
                            self._barge_in_in_progress = False
                            self._noise_rms_sum += rms
                            self._noise_frame_count += 1
                        if self._frame_log is not None:
                            self._log_frame(now, rms, rms, mulaw_bytes)
                    return False  # handled above — do not double-send to STT
                else:
                    self._barge_in_in_progress = False
                    self._barge_gate.reset()
                    if rms > self._barge_rms_threshold:
                        self._signal_rms_sum += rms
                        self._signal_frame_count += 1
                    else:
                        self._noise_rms_sum += rms
                        self._noise_frame_count += 1
                    if self._frame_log is not None:
                        # Ungated window: no echo possible, clean == raw.
                        self._log_frame(now, rms, rms, mulaw_bytes)
                    await self._stt.send_audio(mulaw_bytes)
                    if self._floor is not None:
                        await self._floor.frame(rms, self._barge_gate.threshold, len(mulaw_bytes)/8, False)

        elif event == "stop":
            logger.info("Twilio 'stop' event — call ended by carrier")
            return True

        elif event == "mark":
            mark_name = msg.get("mark", {}).get("name", "")
            logger.debug("Twilio mark echo: %s", mark_name)
            if mark_name in self._pending_marks:
                sent_t, snippet = self._pending_marks.pop(mark_name)
                rtt_ms = (time.monotonic() - sent_t) * 1000
                if mark_name.startswith("tts_s"):
                    # Per-sentence playback confirmation (Phase 3): exact heard
                    # progress. Must NOT stamp _tts_ended_at — playback of the
                    # remaining sentences continues; only tts_end_ marks anchor
                    # the silence timer.
                    self._sentences_played += 1
                    self._audio_log.emit(
                        time.monotonic(), "sentence_played",
                        mark=mark_name, rtt_ms=round(rtt_ms),
                        sentences_played=self._sentences_played,
                    )
                    return False
                logger.info(
                    "Audio delivery confirmed | mark=%s rtt_ms=%.0f utterance=%r"
                    " — Twilio played through this TTS stream",
                    mark_name, rtt_ms, snippet,
                )
                # Twilio has played through all audio up to this mark.
                # Start the silence timer only now — not when chunks were sent.
                self._tts_ended_at = time.monotonic()
                self._close_ledger(self._tts_ended_at)
                if self._floor is not None:
                    await self._floor.playback_finished()
                # The caller has now HEARD the turn. This is the moment their
                # silence starts meaning something, so the window restarts here
                # — unless what just played was the ladder's own probe.
                self._note_playout_end(
                    reset_window=self._is_turn_audio(mark_name.removeprefix("tts_end_")))
                # §Phase 3: the mark round-trip is the outbound observation lead.
                self._outbound_offset.observe(rtt_ms / 1000.0)
                # The moment the caller actually FINISHED HEARING the response —
                # the gap back to tts_end is the jitter-buffer lead.
                self._audio_log.emit(
                    self._tts_ended_at, "mark_echo", mark=mark_name, rtt_ms=round(rtt_ms),
                )

        return False

    async def send_held_response(self, text: str, audio_id: str, **kwargs) -> DeliveryReport | None:
        """The voicemail message (VOICEMAIL_DETECTION_DESIGN.md §6): after
        the line-quiet hold, take the floor from the machine and play
        protected, absorb-all. Never `allow_playback`: its release needs an
        end-of-turn followed by quiet, and a box's beep after the final
        makes that unreachable (call 4108c2eb)."""
        await self.wait_line_quiet()
        if self._floor is None:
            return await self._send_response(text, audio_id, True, True)
        async with self._playback_lock:
            self._floor.take_floor_from_machine(audio_id)
            return await self._send_response(text, audio_id, True, True)

    async def send_response(
        self, text: str, audio_id: str, protected: bool = False, absorb_barge: bool = False,
    ) -> DeliveryReport | None:
        if self._floor is None:
            return await self._send_response(text, audio_id, protected, absorb_barge)
        generation = self._floor.generation
        async with self._playback_lock:
            if not await self._floor.allow_playback(generation, audio_id=audio_id):
                return DeliveryReport(audio_id=audio_id, status='truncated', chunks_sent=0,
                                      audio_ms=0, est_total_ms=estimate_total_ms(text), mark_pending=False)
            return await self._send_response(text, audio_id, protected, absorb_barge)

    async def _send_response(
        self, text: str, audio_id: str, protected: bool = False,
        absorb_barge: bool = False,
    ) -> DeliveryReport | None:
        """Stream TTS audio to Twilio as mulaw 8kHz frames.

        protected: intro/farewell protection (barge-in-compliance-protection):
        this playout plays to completion — barge-in never cuts it (safety
        phrases excepted; backchannels absorb; substantive speech queues and
        is handled right after). The flag lives for the PLAYOUT lifetime: it
        is replaced on the next send_response, so it also covers the
        ~lead-sized drain tail after this call returns.

        Returns a DeliveryReport of what actually streamed (complete/truncated/
        failed); None when nothing was deliverable. A post-return drain-path
        clear is reported separately as a DrainCleared event.
        """
        if not text or not text.strip():
            logger.warning("send_response called with empty text (audio_id=%s)", audio_id)
            return None

        if not self._stream_sid:
            logger.warning("send_response called before streamSid received — dropping (audio_id=%s)", audio_id)
            return None

        self._last_audio_id = audio_id
        self._protected_playout = protected
        # Absorb-all (intro) vs defer (farewell) barge policy for THIS playout.
        self._absorb_playout = protected and absorb_barge
        if protected:
            self._audio_log.emit(time.monotonic(), "protected_playout", audio_id=audio_id)
        # §A delivery ledger: playout starts NOW; end_t lands with the mark echo.
        self._delivery_ledger.append(
            {"audio_id": audio_id, "text": text, "start_t": time.monotonic(), "end_t": None}
        )
        del self._delivery_ledger[:-20]  # bounded
        # Semantic barge-in: a new playout supersedes any open yield window (its
        # heard/unheard context is stale). _absorbing_utterance is kept — the
        # absorbed utterance's final EOT may still be in flight and must swallow.
        self._last_speak_text = text
        if self._yield_task is not None and not self._yield_task.done():
            self._yield_task.cancel()
        self._graceful_yield = False
        self._current_sentences = []
        self._sentences_played = 0
        self._tts_cancel.clear()
        self._tts_playing = True
        if self._clocks is not None:
            # Tell the clocks WHAT is playing: if the mark never echoes,
            # the watchdog stands in for it and needs the same real-turn
            # vs probe distinction the mark-echo path applies.
            self._clocks.note_playout_start(self._is_turn_audio(audio_id))
        # Barge-in gate: start the grace window and reset the debounce streak so
        # a streak accumulated at the tail of the previous turn can't carry over.
        self._tts_started_at = time.monotonic()
        if self._line is not None:
            self._line.note_playout_start()
        self._barge_gate.on_tts_start(self._tts_started_at)
        self._audio_log.emit(
            self._tts_started_at, "tts_start", audio_id=audio_id, text=text[:60],
        )
        if self._ambience is not None:
            # Entering speech: the think-gap is over. Close it (stops the paced
            # loop so it can't pin Twilio's buffer), duck the room bed, and kill
            # any typing (never over our own voice).
            if self._gap_active:
                self._audio_log.emit(time.monotonic(), "gap_close")
            self._gap_active = False
            self._typing_arm_deadline = 0.0
            if self._ambience.typing_state != TypingState.IDLE:
                self._audio_log.emit(time.monotonic(), "typing_stop")
            self._ambience.stop_typing()
            self._ambience.set_speaking(True)

        chunks_sent, had_error = 0, True
        # Frames handed to Twilio across EVERY attempt of this playout. Distinct
        # from chunks_sent, which a retry overwrites: the first attempt's frames
        # are still buffered at the telco and still playing to the caller, so
        # they belong in the clock's picture of how long this playout runs.
        buffered_chunks = 0
        _s_total, _s_sent, _heard = 0, 0, ""
        self._fc_ms = None
        self._fc_t = None
        try:
            (
                chunks_sent, had_error, _s_total, _s_sent, _heard,
            ) = await self._stream_once(text, audio_id)
            buffered_chunks = chunks_sent
            if had_error:
                await asyncio.sleep(0.2)
                # RECONNECT BEFORE RETRYING. The socket is opened once per call
                # and reused, so an error here usually means it is dead — and a
                # retry on a dead socket fails in about a millisecond, which is
                # exactly what happened on call c51a075a: a keepalive ping
                # timeout (1011) killed the connection 64s into a long turn, and
                # then the apology re-speak, its retry, and the silence probe
                # each streamed 0 frames into the same corpse. The logs said
                # "re-speaking with apology"; the caller heard nothing for the
                # rest of the call.
                reopened = await self._tts.reconnect()
                if not reopened:
                    logger.error(
                        "TTS reconnect failed (audio_id=%s) — caller cannot hear "
                        "us; not retrying into a dead socket", audio_id,
                    )
                    self._tts_mute_since = self._tts_mute_since or time.monotonic()
                else:
                    retry_text = self._retry_text(text, chunks_sent,
                                                  _s_total, _s_sent, audio_id)
                    if not retry_text:
                        # Every sentence had already been handed to the telco
                        # when the socket died, so the caller heard the whole
                        # utterance. Re-speaking any of it would be a repeat of
                        # something they just heard, behind an apology for
                        # nothing.
                        logger.warning(
                            "TTS stream errored after all %d sentences were "
                            "sent (audio_id=%s) — the caller heard it; not "
                            "re-speaking", _s_total, audio_id,
                        )
                        had_error = False
                    else:
                        # The failed attempt's sentence accounting describes
                        # the attempt, not the re-speak. Left in place it makes
                        # the report say sentences=3/4 over the retry's frame
                        # count, and a SHORT-SENTENCE count is read as a
                        # truncation: call 18b3f7a3 logged a barge-in the
                        # caller never made ("Barge-in in_flight, heard 43.7s")
                        # on a re-speak that completed cleanly. What replaces
                        # it is the RETRY's own accounting, below.
                        (
                            chunks_sent, had_error, _s_total, _s_sent, _heard,
                        ) = await self._stream_once(retry_text, audio_id)
                        buffered_chunks += chunks_sent
                # Track how long the caller has been unable to hear us. Every
                # subsequent errored turn reconnects again, so a transient
                # outage self-heals on the next speak; this marker is what makes
                # a PERSISTENT one visible instead of silent.
                if chunks_sent > 0 and not had_error:
                    if self._tts_mute_since is not None:
                        logger.info(
                            "TTS audio restored after %.1fs mute",
                            time.monotonic() - self._tts_mute_since)
                    self._tts_mute_since = None
                elif self._tts_mute_since is None:
                    self._tts_mute_since = time.monotonic()
        finally:
            self._tts_playing = False
            self._audio_log.emit(
                time.monotonic(), "tts_end",
                audio_id=audio_id, cancelled=self._tts_cancel.is_set(),
            )
            if self._ambience is not None:
                # Speech over: unduck the room bed. The paced loop resumes
                # emitting ambience-only frames now that _tts_playing is False.
                self._ambience.set_speaking(False)
            mark_name = f"tts_end_{audio_id}"
            if self._floor is not None and self._tts_cancel.is_set():
                # The controller already cleared the audible tail. Do not
                # restart a buffered-playout watchdog for discarded frames.
                self._pending_marks.clear()
            elif mark_name in self._pending_marks:
                # Mark was sent — hold _tts_ended_at at 0.0 (and the clocks in
                # "playing") until Twilio echoes the mark, confirming playback.
                # Tighten the clock watchdog to the known audio length so a
                # swallowed mark cannot freeze the silence ladder (audit P3).
                # The length is EVERY attempt's frames, not the last one's:
                # after a mid-stream retry chunks_sent describes only the
                # re-speak, and on call 18b3f7a3 that understated a live
                # playout by 40.9s — the watchdog then ended a playout 0.12s
                # after the retry finished sending, with 43.68s still to play,
                # and the ladder probed over the agent's own voice.
                self._tts_ended_at = 0.0
                if self._clocks is not None:
                    self._clocks.note_playout_sent(buffered_chunks * 20.0)
            elif buffered_chunks > 0:
                # FAILED MID-STREAM, but the frames already sent are buffered at
                # Twilio and are still playing to the caller. Treating this as
                # "no buffered audio" started the silence timer while the agent
                # was, from the caller's side, still talking: on call c51a075a
                # 65 SECONDS of audio (3256 frames) had been handed to Twilio
                # when the socket died, and "Are you still there?" was asked
                # ~6s later, over the top of it.
                #
                # buffered_chunks, not chunks_sent: when the socket dies and the
                # RETRY sends nothing, chunks_sent is 0 while the first attempt's
                # audio is still playing — the exact c51a075a shape, which fell
                # through to the else-branch below and started the timer anyway.
                #
                # Bound it the same way the mark path does — the watchdog ends
                # the playout at audio-length + margin if no mark echoes — so a
                # lost mark still cannot freeze the ladder (audit P3).
                self._tts_ended_at = 0.0
                if self._clocks is not None:
                    self._clocks.note_playout_sent(buffered_chunks * 20.0)
            else:
                # Cancelled or failed before any audio reached Twilio — nothing
                # is buffered, so the silence timer starts now.
                self._tts_ended_at = time.monotonic()
                self._close_ledger(self._tts_ended_at)
                self._note_playout_end(reset_window=self._is_turn_audio(audio_id))

        cancelled = self._tts_cancel.is_set()
        # A graceful sentence-boundary yield (Phase 3) is also a truncation:
        # sentences beyond the yield were never spoken — the delivery interrupt
        # (Class A/B) and §4f read it exactly like a cut, with an EXACT boundary.
        yielded_short = 0 < _s_total and _s_sent < _s_total and not had_error
        status = (
            "truncated" if (cancelled or yielded_short)
            else ("failed" if had_error else "complete")
        )
        return DeliveryReport(
            audio_id=audio_id,
            status=status,
            chunks_sent=chunks_sent,
            audio_ms=chunks_sent * 20.0,   # Twilio frames are 20 ms mulaw
            est_total_ms=estimate_total_ms(text),
            mark_pending=f"tts_end_{audio_id}" in self._pending_marks,
            sentences_total=_s_total,
            sentences_sent=_s_sent,
            heard_text=_heard,
            first_chunk_ms=self._fc_ms,
            first_chunk_t=self._fc_t,
        )

    async def _stream_once(self, text: str, audio_id: str
                           ) -> tuple[int, bool, int, int, str]:
        """One streaming attempt, whichever path this call is configured for.

        Returns (chunks_sent, had_error, sentences_total, sentences_sent,
        heard_text); the burst path counts no sentences and reports zeros.

        The RETRY comes through here too, which it did not used to: the retry
        fell back to burst on the reasoning that robustness beat pacing. That
        reasoning was inverted — burst was surviving because a paced consumer
        was what killed the socket in the first place (e26edef), and the burst
        re-speak handed Twilio 43.68s of audio in 20s, past the point where
        barge-in can cut it. On call 18b3f7a3 the caller sat through 44
        unstoppable seconds of a repeat. With the receive queue unbounded,
        pacing is safe on both attempts, and the re-speak stays interruptible.
        """
        if settings.tts_pacing_mode == "sentence" and self._tts_encoding == "mulaw":
            return await self._do_tts_stream_sentences(text, audio_id)
        chunks_sent, had_error = await self._do_tts_stream(text, audio_id)
        return chunks_sent, had_error, 0, 0, ""

    def _retry_text(self, text: str, chunks_sent: int, s_total: int,
                    s_sent: int, audio_id: str) -> str:
        """What to say after a failed attempt — "" when there is nothing left.

        Nothing reached the caller: say the whole thing again, no apology (they
        heard no fragment to apologise for).

        Audio reached the caller: apologise and say what they have NOT heard.
        The whole utterance used to be repeated, which is how one 45s answer
        became 85 seconds of the same content (call 18b3f7a3) — and the repeat
        arrives AFTER the frames already buffered at the telco, so re-speaking
        the part they just heard is heard twice, back to back. Sentence counts
        make the boundary exact; without them (burst) the full text is still
        the only safe answer.
        """
        if chunks_sent == 0:
            logger.warning(
                "TTS stream sent 0 chunks (audio_id=%s), retrying once", audio_id
            )
            return text
        if not s_total:
            logger.warning(
                "TTS mid-stream disconnect after %d chunks (audio_id=%s), no "
                "sentence boundaries — re-speaking in full with apology",
                chunks_sent, audio_id,
            )
            return _RESPEAK_APOLOGY + text
        remainder = " ".join(split_sentences(text)[s_sent:]).strip()
        if not remainder:
            return ""
        logger.warning(
            "TTS mid-stream disconnect after %d chunks (audio_id=%s), "
            "re-speaking %d of %d sentences with apology",
            chunks_sent, audio_id, s_total - s_sent, s_total,
        )
        return _RESPEAK_APOLOGY + remainder

    async def _interruptible_tts(self, stream, cancelled_item):
        """Wake the player on cancellation even when the provider is silent."""
        if self._floor is None:
            async for item in stream:
                yield item
            return
        iterator = stream.__aiter__()
        cancel = asyncio.create_task(self._tts_cancel.wait())
        next_chunk = None
        try:
            while True:
                next_chunk = asyncio.create_task(iterator.__anext__())
                done, _ = await asyncio.wait((next_chunk, cancel), return_when=asyncio.FIRST_COMPLETED)
                if cancel in done:
                    next_chunk.cancel()
                    await asyncio.gather(next_chunk, return_exceptions=True)
                    yield cancelled_item
                    return
                try:
                    yield next_chunk.result()
                except StopAsyncIteration:
                    return
        finally:
            cancel.cancel()
            if next_chunk is not None and not next_chunk.done():
                next_chunk.cancel()
            await asyncio.gather(cancel, *([next_chunk] if next_chunk is not None else []), return_exceptions=True)
            await iterator.aclose()

    async def _do_tts_stream_sentences(
        self, text: str, audio_id: str,
    ) -> tuple[int, bool, int, int, str]:
        """Bounded playout buffer (Phase 3, full-duplex-semantic-barge-in §6).

        Sentence-segmented multi-flush TTS + PACED release: the sender stays at
        most tts_buffer_lead_ms ahead of real playback time, so Twilio's buffer
        never holds more than the lead. Consequences:
        - a hard barge cut loses <= the lead (~0.5 s), not the whole reply;
        - a graceful yield (_graceful_yield, set by answer_to_heard) stops at
          the next SENTENCE BOUNDARY — the current sentence finishes naturally,
          no destructive clear;
        - a per-sentence mark (tts_s{i}_{audio_id}) confirms each sentence
          PLAYED — delivered/heard becomes exact.

        Returns (chunks_sent, had_error, sentences_total, sentences_sent,
        heard_text) where heard_text = the fully-SENT sentences joined.
        """
        sentences = split_sentences(text)
        self._current_sentences = sentences
        lead_ms = float(settings.tts_buffer_lead_ms)
        chunks_sent = 0
        total_sent_bytes = 0
        sentences_sent = 0
        cur_seg = 0
        pending = bytearray()
        first_frame_t: float | None = None
        _cancelled = False
        _yielded = False
        _had_error = False
        self._rec_tts_start_ts = 0.0
        self._rec_tts_byte_offset = 0
        _t0 = time.monotonic()

        async def _flush_tail_and_mark(seg_idx: int) -> None:
            nonlocal chunks_sent, total_sent_bytes, sentences_sent
            if pending:
                frame = bytes(pending)
                await self._send_media_frame(frame)
                total_sent_bytes += len(frame)
                chunks_sent += 1
                pending.clear()
            mark = f"tts_s{seg_idx}_{audio_id}"
            self._pending_marks[mark] = (time.monotonic(), sentences[seg_idx][:80])
            await self._send_mark(mark)
            sentences_sent = seg_idx + 1

        try:
            async for seg, chunk, first_chunk_latency in self._interruptible_tts(
                    self._tts.stream_segments(sentences), (0, b'', None)):
                if self._tts_cancel.is_set():
                    logger.debug("TTS interrupted (barge-in, sentence mode) — audio_id=%s", audio_id)
                    await self._tts.cancel()
                    pending.clear()
                    self._pending_marks.clear()
                    _cancelled = True
                    break

                if seg != cur_seg:
                    # ── Sentence boundary ─────────────────────────────────────
                    await _flush_tail_and_mark(cur_seg)
                    cur_seg = seg
                    if self._graceful_yield:
                        # answer_to_heard: the heard clause was answered — stop
                        # queueing further sentences; what was sent finishes
                        # playing. NO Twilio clear.
                        logger.info(
                            "TTS graceful yield at sentence boundary | sent=%d/%d audio_id=%s",
                            sentences_sent, len(sentences), audio_id,
                        )
                        self._audio_log.emit(
                            time.monotonic(), "tts_yield",
                            audio_id=audio_id, sentences_sent=sentences_sent,
                        )
                        await self._tts.cancel()
                        _yielded = True
                        break

                if first_chunk_latency is not None and self._fc_ms is None:
                    # Metrics inversion: report timings via DeliveryReport; the
                    # handler owns emission.
                    self._fc_ms = first_chunk_latency
                    self._fc_t = time.monotonic()

                pending.extend(chunk)
                while len(pending) >= _TWILIO_TTS_FRAME_BYTES:
                    frame = bytes(pending[:_TWILIO_TTS_FRAME_BYTES])
                    if self._ambience is not None:
                        lin = audioop.ulaw2lin(frame, 2)
                        mixed = self._ambience.next_frame(_TWILIO_TTS_FRAME_BYTES, tts_frame=lin)
                        frame = audioop.lin2ulaw(mixed, 2)

                    # ── PACED RELEASE: hold Twilio's buffer at <= the lead ────
                    if first_frame_t is None:
                        first_frame_t = time.monotonic()
                    audio_sent_ms = total_sent_bytes / 8.0     # mulaw 8 kHz: 8 B/ms
                    ahead_ms = audio_sent_ms - (time.monotonic() - first_frame_t) * 1000
                    if ahead_ms > lead_ms:
                        await asyncio.sleep((ahead_ms - lead_ms) / 1000.0)
                        if self._tts_cancel.is_set():
                            break   # cancel arrived during the pacing sleep

                    await self._send_media_frame(frame)
                    del pending[:_TWILIO_TTS_FRAME_BYTES]
                    total_sent_bytes += _TWILIO_TTS_FRAME_BYTES
                    chunks_sent += 1

            if not _cancelled and not self._tts_cancel.is_set():
                if not _yielded:
                    # Final sentence's tail + its per-sentence mark.
                    await _flush_tail_and_mark(cur_seg)
                # End-of-playout mark — SAME semantics as burst mode (echo →
                # _tts_ended_at; pending → playback-drain window).
                mark_name = f"tts_end_{audio_id}"
                snippet = text[:80] + "…" if len(text) > 80 else text
                self._pending_marks[mark_name] = (time.monotonic(), snippet)
                await self._send_mark(mark_name)

        except Exception as exc:
            logger.error("TTS streaming error (sentence mode): %s", exc)
            _had_error = True

        finally:
            elapsed_ms = (time.monotonic() - _t0) * 1000
            audio_ms = total_sent_bytes * 1000 // 8000
            logger.info(
                "TTS sentence stream done | audio_id=%s sentences=%d/%d frames=%d"
                " audio_ms=%d elapsed_ms=%.0f cancelled=%s yielded=%s error=%s",
                audio_id, sentences_sent, len(sentences), chunks_sent,
                audio_ms, elapsed_ms, _cancelled, _yielded, _had_error,
            )
            if _cancelled:
                await self._send_clear()

        heard_text = " ".join(sentences[:sentences_sent])
        return chunks_sent, _had_error, len(sentences), sentences_sent, heard_text

    async def _do_tts_stream(self, text: str, audio_id: str) -> tuple[int, bool]:
        """Inner TTS streaming loop. Returns (chunks_sent, had_error).

        chunks_sent counts frames actually sent via _send_raw. The invariant
        chunks_sent == 0 ⟺ caller heard nothing is preserved by the buffering
        logic — we increment only on _send_raw, never on buffer accumulation.
        """
        first_chunk = True
        chunks_sent = 0
        total_sent_bytes = 0
        pending = bytearray()
        ratecv_state = None         # used only in legacy linear16 path
        self._rec_tts_start_ts = 0.0
        self._rec_tts_byte_offset = 0
        _last_frame_t: float | None = None
        _gap_count = 0
        _cancelled = False
        _had_error = False
        _t0 = time.monotonic()

        try:
            async for chunk, first_chunk_latency in self._interruptible_tts(
                    self._tts.stream(text), (b'', None)):
                if self._tts_cancel.is_set():
                    logger.debug("TTS interrupted (barge-in, Twilio) — audio_id=%s", audio_id)
                    await self._tts.cancel()
                    pending.clear()
                    self._pending_marks.clear()
                    _cancelled = True
                    break

                if first_chunk_latency is not None and self._fc_ms is None:
                    self._fc_ms = first_chunk_latency

                if first_chunk:
                    # Metrics inversion: report timings via DeliveryReport; the
                    # handler owns emission.
                    self._fc_t = time.monotonic()
                    first_chunk = False

                # ── Produce mulaw bytes ───────────────────────────────────────
                if self._tts_encoding == "mulaw":
                    # Phone-optimised path: Deepgram already output mulaw 8kHz.
                    mulaw_bytes = chunk
                else:
                    # Legacy fallback: linear16 24kHz → resample → mulaw 8kHz.
                    if len(chunk) % _TTS_SAMPLE_WIDTH:
                        chunk = chunk[:len(chunk) - (len(chunk) % _TTS_SAMPLE_WIDTH)]
                    if not chunk:
                        continue
                    pcm_8k, ratecv_state = audioop.ratecv(
                        chunk, _TTS_SAMPLE_WIDTH, 1,
                        _TTS_SOURCE_RATE, _TTS_TARGET_RATE, ratecv_state,
                    )
                    mulaw_bytes = audioop.lin2ulaw(pcm_8k, _TTS_SAMPLE_WIDTH)

                # ── Buffer and emit at target frame size ──────────────────────
                pending.extend(mulaw_bytes)
                while len(pending) >= _TWILIO_TTS_FRAME_BYTES:
                    frame = bytes(pending[:_TWILIO_TTS_FRAME_BYTES])

                    # Mix the (ducked) room bed into the speech frame so the
                    # background doesn't vanish the instant the agent talks.
                    # μ-law is nonlinear — decode to linear16, mix, re-encode.
                    if self._ambience is not None:
                        lin = audioop.ulaw2lin(frame, 2)
                        mixed = self._ambience.next_frame(_TWILIO_TTS_FRAME_BYTES, tts_frame=lin)
                        frame = audioop.lin2ulaw(mixed, 2)

                    # ── (1) Frame gap detection ───────────────────────────────
                    now = time.monotonic()
                    if _last_frame_t is not None:
                        gap_ms = (now - _last_frame_t) * 1000
                        if gap_ms > _TTS_FRAME_GAP_WARN_MS:
                            _gap_count += 1
                            # Buffer debt = audio already sent minus wall time elapsed.
                            # When debt > gap we are running ahead of real-time and
                            # Twilio's buffer can absorb this send-side pause; no underrun.
                            audio_sent_ms = total_sent_bytes * 1000 / 8000
                            elapsed_so_far_ms = (now - _t0) * 1000
                            buffer_debt_ms = audio_sent_ms - elapsed_so_far_ms
                            if buffer_debt_ms < gap_ms:
                                logger.warning(
                                    "TTS frame gap | gap_ms=%.0f buffer_ahead_ms=%.0f"
                                    " frames_sent=%d audio_id=%s"
                                    " — Twilio jitter buffer may underrun",
                                    gap_ms, buffer_debt_ms, chunks_sent, audio_id,
                                )
                            else:
                                logger.debug(
                                    "TTS frame gap (benign) | gap_ms=%.0f"
                                    " buffer_ahead_ms=%.0f frames_sent=%d audio_id=%s",
                                    gap_ms, buffer_debt_ms, chunks_sent, audio_id,
                                )
                    _last_frame_t = now

                    await self._send_media_frame(frame)
                    del pending[:_TWILIO_TTS_FRAME_BYTES]
                    total_sent_bytes += _TWILIO_TTS_FRAME_BYTES
                    chunks_sent += 1

                    # ── (3) Send queue depth ──────────────────────────────────
                    if self._qsize_fn is not None:
                        depth = self._qsize_fn()
                        if depth > _SEND_QUEUE_DEPTH_WARN:
                            logger.warning(
                                "Send queue backed up | depth=%d frames_sent=%d audio_id=%s"
                                " — ws.send_text lagging behind frame production",
                                depth, chunks_sent, audio_id,
                            )

            # ── Post-loop: handle cancel or flush remaining audio ─────────────
            if _cancelled:
                await self._send_clear()
            else:
                if pending:
                    # Tail flush — remaining bytes that didn't fill a full frame.
                    frame = bytes(pending)
                    await self._send_media_frame(frame)
                    total_sent_bytes += len(frame)
                    chunks_sent += 1

                # ── (4) Mark for delivery confirmation ────────────────────────
                # Sent unconditionally on clean completion so rtt_ms is always
                # recorded. Previously nested inside the tail-flush branch, which
                # meant frame-aligned streams (no tail) never sent the mark.
                mark_name = f"tts_end_{audio_id}"
                snippet = text[:80] + "…" if len(text) > 80 else text
                self._pending_marks[mark_name] = (time.monotonic(), snippet)
                await self._send_mark(mark_name)

        except Exception as exc:
            logger.error("TTS streaming error (Twilio): %s", exc)
            _had_error = True

        finally:
            # ── (2) Per-utterance stream summary ─────────────────────────────
            elapsed_ms = (time.monotonic() - _t0) * 1000
            audio_ms = total_sent_bytes * 1000 // 8000
            ratio = audio_ms / elapsed_ms if elapsed_ms > 0 else 0.0
            logger.info(
                "TTS stream done | audio_id=%s frames=%d audio_ms=%d"
                " elapsed_ms=%.0f ratio=%.2f gaps=%d cancelled=%s error=%s",
                audio_id, chunks_sent, audio_ms,
                elapsed_ms, ratio, _gap_count, _cancelled, _had_error,
            )

        return chunks_sent, _had_error

    async def _send_media_frame(self, mulaw_bytes: bytes) -> None:
        """Base64-encode mulaw bytes and dispatch a Twilio media frame."""
        if self._recorder is not None:
            if self._rec_tts_start_ts == 0.0:
                self._rec_tts_start_ts = time.monotonic()
            rec_ts = self._rec_tts_start_ts + self._rec_tts_byte_offset / 8000.0
            self._recorder.add_agent_chunk(rec_ts, mulaw_bytes)
            self._rec_tts_byte_offset += len(mulaw_bytes)
        if self._aec is not None:
            self._aec.feed_reference(mulaw_bytes)
        payload = base64.b64encode(mulaw_bytes).decode("ascii")
        await self._send_raw(json.dumps({
            "event": "media",
            "streamSid": self._stream_sid,
            "media": {"payload": payload},
        }))

    async def _send_mark(self, name: str) -> None:
        """Send a Twilio mark frame. Twilio echoes it back after playing through
        that position in the audio stream, confirming delivery and playback."""
        if not self._stream_sid:
            return
        await self._send_raw(json.dumps({
            "event": "mark",
            "streamSid": self._stream_sid,
            "mark": {"name": name},
        }))

    # ── Helpers ───────────────────────────────────────────────────────────────

    # ── Full-duplex semantic barge-in (Phase 1) ───────────────────────────────
    # docs/full-duplex-semantic-barge-in.md §4. The energy confirm opens a
    # yield-pending window instead of cutting; heuristic floor + a time-capped
    # Cerebras verdict decide absorb (keep playing, swallow the turn) vs cut.

    def _playout_heard_split(self) -> tuple[str, str]:
        """(heard, unheard) of the current playout.

        Sentence mode (Phase 3): EXACT — per-sentence mark echoes count played
        sentences; the boundary is precise to the sentence. Burst/fallback:
        proportional estimate by elapsed time.
        """
        text = self._last_speak_text
        if not text:
            return "", ""
        if self._current_sentences:
            k = self._sentences_played
            heard = " ".join(self._current_sentences[:k])
            unheard = " ".join(self._current_sentences[k:])
            return heard, unheard
        elapsed_ms = (
            (time.monotonic() - self._tts_started_at) * 1000
            if self._tts_started_at else 0.0
        )
        heard = estimate_heard_text(text, elapsed_ms, estimate_total_ms(text))
        return heard, text[len(heard):].strip()

    def _emit_barge_cut_telemetry(self, raw_rms: int, clean_rms: int) -> None:
        drain = not self._tts_playing
        logger.info(
            "BARGE_IN event | raw_rms=%d clean_rms=%d threshold=%d"
            " confirm=%s mode=%s call_id=%s",
            raw_rms, clean_rms, self._barge_gate.threshold,
            self._barge_gate.cfg.confirm,
            "buffer_drain" if drain else "stream_in_flight",
            self._call_id,
        )
        _wf = self._cfg.workflow_id if self._cfg is not None else ""
        metrics_sink.emit(
            "barge_in_event", _wf, float(clean_rms),
            instance_id=self._call_id,
        )

    async def _cut_playback(self) -> None:
        """Yield the floor: cancel the in-flight stream or clear the drain buffer."""
        if not self._tts_playing:
            # TTS streaming done but Twilio still playing buffered audio —
            # discard it and end the drain state (a DrainCleared event records
            # the late truncation for the handler).
            await self._clear_buffered_playback()
        else:
            self._tts_cancel.set()

    async def _run_protected_window(self) -> None:
        """Yield window for a PROTECTED playout (intro/farewell): the only
        actionable verdict is `safety` (immediate cut). Backchannels/noise
        absorb as usual; substantive speech is DEFERRED — neither cut nor
        swallowed, it queues and is answered right after the playout. No
        Cerebras call is made (nothing else needs judging)."""
        t0 = time.monotonic()
        window_s = settings.semantic_barge_window_ms / 1000.0
        try:
            # ── Absorb-all playouts (the opening; 2026-08-02) ────────────────
            # Every verdict except `safety` absorbs — nothing queues behind
            # the greeting. The window is held only long enough to catch a
            # safety phrase or gather text worth putting in the audit entry.
            if self._absorb_playout:
                while (time.monotonic() - t0) < window_s:
                    text = self._last_interim_text.strip()
                    if heuristic_verdict(
                        text,
                        backchannel_max_tokens=settings.semantic_barge_backchannel_max_tokens,
                        substantive_min_tokens=settings.semantic_barge_substantive_min_tokens,
                        safety_phrases=self._safety_phrases,
                        floor_phrases=self._floor_phrases,
                        playout_text=self._last_speak_text,
                    ) == "safety":
                        await self._act_on_barge_verdict(
                            "safety", "protected_intro", text, t0)
                        return
                    if text:
                        break
                    await asyncio.sleep(0.05)
                await self._act_on_barge_verdict(
                    "intro_absorb", "protected_intro",
                    self._last_interim_text.strip(), t0)
                return
            while (time.monotonic() - t0) < window_s:
                text = self._last_interim_text.strip()
                h = heuristic_verdict(
                    text,
                    backchannel_max_tokens=settings.semantic_barge_backchannel_max_tokens,
                    substantive_min_tokens=settings.semantic_barge_substantive_min_tokens,
                    safety_phrases=self._safety_phrases,
                    floor_phrases=self._floor_phrases,
                    playout_text=self._last_speak_text,
                )
                if h == "safety":
                    await self._act_on_barge_verdict(h, "protected_heuristic", text, t0)
                    return
                if h == "backchannel":
                    await self._act_on_barge_verdict(h, "protected_heuristic", text, t0)
                    return
                if h in ("substantive_interrupt", "floor_request"):
                    # Defer: no cut, no swallow — the final will queue and be
                    # answered right after the protected playout finishes.
                    # A floor request defers too: only `safety` may cut a
                    # protected playout (the recorded-line disclosure).
                    self._audio_log.emit(
                        time.monotonic(), "barge_deferred_protected", text=text[:60],
                    )
                    logger.info(
                        "Protected playout: substantive speech deferred | text=%r", text[:80],
                    )
                    return
                await asyncio.sleep(0.05)
            # Expiry: no_text → absorb as noise; ambiguous → defer.
            text = self._last_interim_text.strip()
            if not text:
                await self._act_on_barge_verdict("no_text", "protected_window_expiry", text, t0)
            else:
                self._audio_log.emit(
                    time.monotonic(), "barge_deferred_protected", text=text[:60],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never kill the frame loop
            logger.warning("protected barge window error (%s) — deferring", exc)

    async def _run_yield_window(self) -> None:
        """The yield-pending window: audio keeps playing while the verdict forms.

        Resolution order: decisive heuristic (0 ms) → Cerebras verdict (capped
        at semantic_barge_llm_timeout_ms) → window expiry. Ambiguous with no
        LLM verdict → ABSORB (user decision: when unsure, keep speaking).

        Protected playouts (intro/farewell) use _run_protected_window instead.
        """
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        window_s = settings.semantic_barge_window_ms / 1000.0
        llm_timeout_s = settings.semantic_barge_llm_timeout_ms / 1000.0
        llm_future = None
        llm_started_at = 0.0
        judged_text = ""
        try:
            while (time.monotonic() - t0) < window_s:
                judged_text = self._last_interim_text.strip()
                h = heuristic_verdict(
                    judged_text,
                    backchannel_max_tokens=settings.semantic_barge_backchannel_max_tokens,
                    substantive_min_tokens=settings.semantic_barge_substantive_min_tokens,
                    safety_phrases=self._safety_phrases,
                    floor_phrases=self._floor_phrases,
                    playout_text=self._last_speak_text,
                )
                if h in DECISIVE_HEURISTICS:
                    await self._act_on_barge_verdict(h, "heuristic", judged_text, t0)
                    return
                if (h == "ambiguous" and llm_future is None and judged_text
                        and self._semantic_judge):
                    heard, unheard = self._playout_heard_split()
                    llm_future = loop.run_in_executor(
                        None, classify_barge,
                        judged_text, heard, unheard, self._last_speak_text,
                    )
                    llm_started_at = time.monotonic()
                if llm_future is not None:
                    if llm_future.done():
                        try:
                            llm_v = llm_future.result()
                        except Exception:  # noqa: BLE001
                            llm_v = None
                        verdict, source = resolve_final(h, llm_v)
                        await self._act_on_barge_verdict(verdict, source, judged_text, t0)
                        return
                    if (time.monotonic() - llm_started_at) > llm_timeout_s:
                        verdict, source = resolve_final("ambiguous", None)  # → timeout_absorb
                        await self._act_on_barge_verdict(verdict, source, judged_text, t0)
                        return
                await asyncio.sleep(0.05)
            # Window expired: no_text → absorb-as-noise; anything undecided → absorb.
            judged_text = self._last_interim_text.strip()
            h = heuristic_verdict(
                judged_text,
                backchannel_max_tokens=settings.semantic_barge_backchannel_max_tokens,
                substantive_min_tokens=settings.semantic_barge_substantive_min_tokens,
                safety_phrases=self._safety_phrases,
                floor_phrases=self._floor_phrases,
                playout_text=self._last_speak_text,
            )
            verdict, source = resolve_final(h, None)
            await self._act_on_barge_verdict(verdict, source, judged_text, t0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the window must never kill the frame loop
            logger.warning("semantic barge window error (%s) — absorbing", exc)
            try:
                await self._act_on_barge_verdict("timeout_absorb", "error", judged_text, t0)
            except Exception:  # noqa: BLE001
                pass

    async def _judge_playback_final(self, text: str) -> None:
        """Open STT feed (docs/stt-open-feed.md): judge a FINAL transcript that
        arrived while our audio is active and no cut/absorb has happened.

        With the feed open, Deepgram transcribes speech the energy gate never
        confirmed — quiet callers, and possibly our own leaked voice (echo).
        Without this rule such a final would run the full pipeline and the
        agent would answer over its own playback. Order of checks:

        1. Echo guard — the transcript is a fragment of what WE were saying →
           absorb with verdict "echo" (always swallowed; logged BARGE_ABSORBED).
        2. The same verdict flow the yield window uses: heuristic floor, then
           the time-capped Cerebras call for the ambiguous middle; the verdict
           is acted on by _act_on_barge_verdict (absorb / graceful yield / cut).
        """
        t0 = time.monotonic()
        if is_echo_of(text, self._last_speak_text):
            self._absorbing_utterance = True
            self._absorb_detail = {
                "text": text, "verdict": "echo", "source": "echo_guard",
                "window_ms": 0, "audio_id": self._last_audio_id,
            }
            self._audio_log.emit(t0, "echo_discarded", text=text[:60])
            logger.info("Echo transcript discarded (open feed) | text=%r", text[:80])
            return
        h = heuristic_verdict(
            text,
            backchannel_max_tokens=settings.semantic_barge_backchannel_max_tokens,
            substantive_min_tokens=settings.semantic_barge_substantive_min_tokens,
            safety_phrases=self._safety_phrases,
            floor_phrases=self._floor_phrases,
            playout_text=self._last_speak_text,
        )
        # ── Protected playout: safety cuts, backchannel/noise absorb, and ────
        # everything else (substantive/ambiguous) DEFERS — the final proceeds
        # untagged, queues, and is answered right after the playout. No
        # Cerebras call while protected.
        if self._protected_playout:
            if self._absorb_playout and h != "safety":
                # Absorb-all playout: a quiet talker's final absorbs too —
                # the one queue door the energy gate cannot close.
                await self._act_on_barge_verdict(
                    "intro_absorb", "protected_playback_final", text, t0)
                return
            if h in ("safety", "backchannel", "no_text"):
                await self._act_on_barge_verdict(h, "protected_playback_final", text, t0)
            else:
                self._audio_log.emit(
                    time.monotonic(), "barge_deferred_protected", text=text[:60],
                )
                logger.info(
                    "Protected playout: final deferred to post-playout | text=%r",
                    text[:80],
                )
            return
        llm_v = None
        if h == "ambiguous":
            heard, unheard = self._playout_heard_split()
            loop = asyncio.get_running_loop()
            try:
                llm_v = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, classify_barge, text, heard, unheard, self._last_speak_text,
                    ),
                    timeout=settings.semantic_barge_llm_timeout_ms / 1000.0,
                )
            except Exception:  # noqa: BLE001 — timeout/error → heuristic fallback
                llm_v = None
        verdict, source = resolve_final(h, llm_v)
        await self._act_on_barge_verdict(verdict, f"playback_final:{source}", text, t0)

    async def _act_on_barge_verdict(
        self, verdict: str, source: str, text: str, window_t0: float,
    ) -> None:
        window_ms = round((time.monotonic() - window_t0) * 1000)
        # ── Protected playout (intro/farewell): only `safety` may cut. ────────
        # Defense in depth — the protected window/judge shouldn't produce other
        # cut verdicts, but any path that does is downgraded to a defer here.
        if (
            self._protected_playout
            and not is_absorb(verdict)
            and verdict != "safety"
        ):
            self._audio_log.emit(
                time.monotonic(), "barge_deferred_protected",
                verdict=verdict, source=source, text=text[:60],
            )
            logger.info(
                "Protected playout: %s verdict deferred (no cut) | source=%s text=%r",
                verdict, source, text[:80],
            )
            return
        if is_absorb(verdict):
            # Keep playing. Mark the utterance for turn-swallowing (§4.5) and
            # re-arm the gate so a future REAL barge can open a fresh window.
            self._absorbing_utterance = True
            self._absorb_detail = {
                "text": text, "verdict": verdict, "source": source,
                "window_ms": window_ms, "audio_id": self._last_audio_id,
            }
            self._barge_gate.reset()
            self._audio_log.emit(
                time.monotonic(), "barge_absorbed",
                verdict=verdict, source=source, text=text[:60],
            )
            logger.info(
                "Semantic barge ABSORBED | verdict=%s source=%s window_ms=%d text=%r",
                verdict, source, window_ms, text[:80],
            )
        elif (
            verdict == "answer_to_heard"
            and settings.tts_pacing_mode == "sentence"
        ):
            # Graceful yield (Phase 3 §6.3): the caller answered the clause they
            # heard. In-flight → finish the CURRENT sentence, drop the rest (no
            # destructive clear). Drain → at most the ~lead is left playing; let
            # it finish. The utterance still processes as a normal turn.
            self._graceful_yield = True
            self._audio_log.emit(
                time.monotonic(), "barge_yield",
                verdict=verdict, source=source, window_ms=window_ms,
            )
            logger.info(
                "Semantic barge YIELD (sentence boundary) | source=%s window_ms=%d text=%r",
                source, window_ms, text[:80],
            )
        else:
            self._audio_log.emit(
                time.monotonic(), "barge_cut",
                verdict=verdict, source=source, window_ms=window_ms,
            )
            logger.info(
                "Semantic barge CUT | verdict=%s source=%s window_ms=%d text=%r",
                verdict, source, window_ms, text[:80],
            )
            self._emit_barge_cut_telemetry(self._candidate_raw_rms, self._candidate_clean_rms)
            await self._cut_playback()

    async def _clear_buffered_playback(self) -> None:
        """Barge-in during the playback-drain window: discard Twilio's buffered
        audio and END the drain state, handing the gate to the echo cooldown
        (whose expiry resets the BargeGate on the next ungated frame).

        Clearing _pending_marks locally matters twice over: it stops
        _playback_draining (the audio is gone — nothing is draining), and it
        makes _tts_ended_at the truthful silence anchor (the mark may still
        echo later; it must not overwrite the actual stop time).
        """
        _drained = self._last_audio_id or None
        await self._send_clear()
        self._pending_marks.clear()
        self._tts_ended_at = time.monotonic()
        self._close_ledger(self._tts_ended_at)
        self._note_playout_end()
        if _drained:
            # Push replacement for consume_drain_clear (phase0 §4.2): the fact
            # is enqueued at the moment it happens; FIFO puts it ahead of the
            # barging caller's final. Always emitted — the handler applies any
            # delivery-gate policy.
            self._enqueue_event(DrainCleared(audio_id=_drained, at=self._tts_ended_at))

    async def _send_clear(self) -> None:
        """Tell Twilio to discard audio it has buffered but not yet played."""
        if not self._stream_sid:
            return
        now = time.monotonic()
        discarded = self._recorder.discard_agent_after(now) if self._recorder is not None else 0
        self._audio_log.emit(now, "twilio_clear", recording_discarded_ms=discarded / 8,
                             recording_cut_basis='clear_dispatch_estimate')
        await self._send_raw(json.dumps({
            "event": "clear",
            "streamSid": self._stream_sid,
        }))
        if self._aec is not None:
            # The discarded audio never plays: its reference frames must not
            # be popped as if it did, or the next reply's reference lags.
            self._aec.discard_reference()

    # ── Phase 0 barge-in measurement (docs/barge-in-tuning.md) ──────────────────

    def _log_frame(self, now: float, raw_rms: int, clean_rms: int, clean_mulaw: bytes) -> None:
        """Append one per-frame measurement row + the post-AEC audio chunk.

        Only called when barge_in_frame_log is on. The audio chunks concatenate
        into the exact mono stream a VAD confirm step would see (post-AEC during
        the echo-gated window; raw — identical — outside it), replayable through
        candidate VADs offline by proto/barge/analyze_frames.py.
        """
        if self._frame_log_t0 == 0.0:
            self._frame_log_t0 = now
        self._frame_log.append((
            round(now - self._frame_log_t0, 3),
            raw_rms,
            clean_rms,
            int(self._tts_playing),
            int(self._gap_active),
            self._barge_gate.streak,
            int(self._barge_gate.in_progress or self._barge_in_in_progress),
            # Live gate view — makes Phase A's adaptive behaviour observable
            # per frame (the EMA trajectory is otherwise invisible, and the
            # margin can't be tuned without it — docs/barge-in-tuning.md).
            self._barge_gate.threshold,
            round(self._barge_gate.noise_floor, 1),
        ))
        self._frame_log_chunks.append(clean_mulaw)

    def _flush_frame_log(self) -> None:
        """Write <call_id>.frames.csv + <call_id>.clean.wav at call end."""
        if self._frame_log is None or not self._frame_log:
            return

        out_dir = Path(settings.recordings_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        call_id = self._call_id

        csv_path = out_dir / f"{call_id}.frames.csv"
        with open(csv_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([
                "t_rel_s", "raw_rms", "clean_rms",
                "tts_playing", "gap_active", "streak", "barge_in_progress",
                "threshold", "noise_floor",
            ])
            w.writerows(self._frame_log)

        wav_path = out_dir / f"{call_id}.clean.wav"
        pcm = audioop.ulaw2lin(b"".join(self._frame_log_chunks), 2)
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_TWILIO_STT_SAMPLE_RATE)
            wf.writeframes(pcm)

        logger.info(
            "barge frame log written | rows=%d audio_s=%.1f csv=%s wav=%s",
            len(self._frame_log), len(pcm) / (2 * _TWILIO_STT_SAMPLE_RATE),
            csv_path, wav_path,
        )

    # ── Ambience (server-side liveliness) ───────────────────────────────────────

    def _arm_typing(self) -> None:
        """Open the think-gap at STT-final: from here until TTS starts, the
        paced loop emits room tone + typing. Typing fires after the (0 by
        default) delay. No-op unless ambience is enabled."""
        if self._ambience is None:
            return
        if not self._gap_active:
            self._audio_log.emit(time.monotonic(), "gap_open")
        self._gap_active = True
        if self._amb_typing:
            self._typing_arm_deadline = time.monotonic() + self._amb_typing_delay

    async def _send_ambience_frame(self, mulaw_bytes: bytes) -> None:
        """Send a between-turn ambience-only media frame.

        Feeds the AEC reference (so the room bed is cancelled from inbound STT,
        not mistaken for caller speech) but deliberately does NOT touch the TTS
        recorder cursor — the per-utterance recording timeline is owned by
        _send_media_frame and must not be advanced by continuous ambience.
        """
        if self._aec is not None:
            self._aec.feed_reference(mulaw_bytes)
        payload = base64.b64encode(mulaw_bytes).decode("ascii")
        await self._send_raw(json.dumps({
            "event": "media",
            "streamSid": self._stream_sid,
            "media": {"payload": payload},
        }))

    async def _ambience_loop(self) -> None:
        """Paced 20 ms emitter that fills the THINK-GAP (STT-final → TTS start)
        with room tone + typing. Emits ONLY while ``_gap_active`` — never in the
        idle period after a response.

        Why gap-only: the TTS path burst-sends frames seconds ahead of real
        time and leans on Twilio's playout buffer. If we emit continuously the
        instant TTS is *sent* (``_tts_playing`` False, but the audio is still
        buffered and unplayed), we refill that buffer at exactly its drain rate,
        pinning it at the burst-lead depth — the caller then hears everything
        delayed by it, and it ratchets up across turns. The gap starts only
        after the caller has listened + spoken for seconds, by which point the
        buffer has drained, so gap frames play concurrently with the agent's
        compute and add ~no latency. Room tone during speech is handled
        separately by mixing into the TTS frames (no extra frames, no lead).
        """
        frame_period = _TWILIO_TTS_FRAME_BYTES / float(_TWILIO_STT_SAMPLE_RATE)  # 160/8000 = 20 ms
        n = _TWILIO_TTS_FRAME_BYTES  # 160 samples = 160 μ-law bytes = 20 ms
        next_t = time.monotonic()
        while not self._amb_stop.is_set():
            now = time.monotonic()
            try:
                if self._stream_sid and self._gap_active and not self._tts_playing:
                    # Fire the armed typing burst once the (0-default) delay passes.
                    if self._typing_arm_deadline and now >= self._typing_arm_deadline:
                        self._ambience.start_typing(self._typing_idx)
                        self._audio_log.emit(now, "typing_start", clip=self._typing_idx)
                        self._typing_idx += 1
                        self._typing_arm_deadline = 0.0
                    lin = self._ambience.next_frame(n)
                    await self._send_ambience_frame(audioop.lin2ulaw(lin, 2))
            except Exception as exc:  # noqa: BLE001 — never let ambience kill the call
                logger.debug("ambience loop frame error (ignored): %s", exc)
            next_t += frame_period
            sleep = next_t - time.monotonic()
            if sleep > 0:
                await asyncio.sleep(sleep)
            else:
                next_t = time.monotonic()  # fell behind → resync to now

    def transport_analytics(self) -> dict:
        rows: dict = {}
        if getattr(self, "_stt_open_ms", None) is not None:
            rows["stt_session_open"] = {
                "stage": "stt", "latency_ms": self._stt_open_ms, "metadata": {},
            }
        if self._stt:
            rows["transcription_quality"] = {
                "stage": "stt", "latency_ms": 0.0,
                "metadata": self._stt.quality_stats(),
            }
        snr_db: float | None = None
        if self._signal_frame_count >= 5 and self._noise_frame_count >= 5:
            avg_signal = self._signal_rms_sum / self._signal_frame_count
            avg_noise = self._noise_rms_sum / self._noise_frame_count
            if avg_noise >= 1:
                snr_db = round(20 * math.log10(avg_signal / avg_noise), 1)
        rows["line_snr_db"] = {
            "stage": "stt", "latency_ms": 0.0,
            "metadata": {
                "snr_db": snr_db,
                "signal_frames": self._signal_frame_count,
                "noise_frames": self._noise_frame_count,
            },
        }
        rows["barge_in_count"] = {
            "stage": "pipeline", "latency_ms": 0.0,
            "metadata": {"count": self._barge_in_attempts},
        }
        rows['barge_in_suppressed_speech'] = {
            'stage': 'pipeline', 'latency_ms': 0.0,
            'metadata': self._audio_log.suppressed_speech_summary(),
        }
        return rows
