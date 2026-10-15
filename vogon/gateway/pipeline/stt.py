"""Deepgram streaming Speech-to-Text — Flux model (SDK v6, v2 endpoint).

Turn-end detection
==================
The v2 API uses a TurnInfo event stream instead of is_final/speech_final:

  TurnInfo.event == "Update"          → on_interim  (partial transcript)
  TurnInfo.event == "StartOfTurn"     → on_interim  (speech started)
  TurnInfo.event == "EagerEndOfTurn"  → on_eager_end (speculative pipeline)
  TurnInfo.event == "TurnResumed"     → on_turn_resumed (cancel speculative work)
  TurnInfo.event == "EndOfTurn"       → on_final    (commit response / TTS)

When eager_eot_threshold is not set, EagerEndOfTurn/TurnResumed are not emitted
and EndOfTurn uses eot_hold_ms before on_final (legacy path).

If the session closes before "EndOfTurn" arrives, the last "EagerEndOfTurn"
text is flushed as a final so the pipeline never stalls.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from deepgram import AsyncDeepgramClient
from deepgram.core.events import EventType
from deepgram.listen.v2.socket_client import V2SocketClientResponse
from deepgram.listen.v2.types import ListenV2Connected, ListenV2FatalError, ListenV2TurnInfo

from vogon.gateway.media_clock import stream_to_monotonic
from vogon.gateway.settings import settings

logger = logging.getLogger(__name__)


def merge_resumed_transcripts(prefix: str, text: str) -> str:
    """Merge a retained pre-resume transcript with a post-resume transcript.

    Pure. Three cases, checked on punctuation-normalized words:
    - the new transcript already CONTAINS the prefix word sequence (Flux kept
      the turn cumulative, or revised it) → use the new transcript as-is;
    - the prefix's word-suffix overlaps the new transcript's word-prefix
      (partial restart) → merge at the maximal overlap;
    - disjoint (full restart — the observed production case) → concatenate.
    """
    if not prefix:
        return text
    if not text:
        return prefix
    p_words, t_words = prefix.split(), text.split()

    def _norm(w: str) -> str:
        return w.lower().strip(".,!?;:'\"")

    pn = [_norm(w) for w in p_words]
    tn = [_norm(w) for w in t_words]
    # Containment: the prefix sequence appears inside the new transcript.
    for start in range(len(tn) - len(pn) + 1):
        if tn[start:start + len(pn)] == pn:
            return text
    # Maximal suffix(prefix)/prefix(text) word overlap.
    for k in range(min(len(pn), len(tn)), 0, -1):
        if pn[-k:] == tn[:k]:
            return " ".join(p_words + t_words[k:])
    return " ".join(p_words + t_words)

OnInterim = Callable[[str, float], Awaitable[None]]
OnFinal = Callable[[str, float, float, list[str]], Awaitable[None]]
OnEagerEnd = Callable[[str, float, float, list[str]], Awaitable[None]]
OnTurnResumed = Callable[[], Awaitable[None]]


class STTSession:
    """Manages a single Deepgram Flux live transcription session (v2 API)."""

    def __init__(
        self,
        on_interim: OnInterim,
        on_final: OnFinal,
        language: str = "en-US",
        keyterms: list[str] | None = None,
        encoding: str = "linear16",
        sample_rate: int = 16000,
        eot_timeout_ms: int | None = None,
        eot_threshold: float | None = None,
        eager_eot_threshold: float | None = None,
        on_eager_end: OnEagerEnd | None = None,
        on_turn_resumed: OnTurnResumed | None = None,
    ) -> None:
        self._on_interim = on_interim
        self._on_final = on_final
        self._on_eager_end = on_eager_end
        self._on_turn_resumed = on_turn_resumed
        self._language = language
        self._keyterms = keyterms or []
        self._encoding = encoding
        self._sample_rate = sample_rate
        self._eot_timeout_ms = eot_timeout_ms if eot_timeout_ms is not None else settings.deepgram_eot_timeout_ms
        self._eot_threshold = eot_threshold if eot_threshold is not None else settings.deepgram_eot_threshold
        self._eager_eot_threshold = eager_eot_threshold
        self._eager_mode = (
            eager_eot_threshold is not None
            and eager_eot_threshold > 0
            and on_eager_end is not None
            and on_turn_resumed is not None
        )
        self._session_start = time.monotonic()
        # §Phase 3 (docs/media_clock / call-event-ordering): the stream-relative
        # start (Deepgram word .start, seconds into the audio) of the most recent
        # utterance's FIRST word — the true onset, before STT delivery lag. None
        # when word timings are unavailable; the transport falls back to the
        # first-interim-arrival proxy.
        self._last_onset_stream_s: float | None = None
        self._connected = False

        # Held open for the session lifetime.
        self._ctx = None
        self._connection = None
        self._listen_task: asyncio.Task | None = None

        # Text from EagerEndOfTurn used as fallback if EndOfTurn never arrives.
        self._pending_text: str = ""
        self._pending_confidence: float = 0.0

        # Resume-aware transcript stitching. Flux SHOULD keep a resumed turn's
        # transcript cumulative (TurnResumed continues the SAME turn), but a
        # production call showed the post-resume transcript RESTARTING from the
        # resume point: eager="Can I pay it…" → TurnResumed → final="on the
        # phone?" — the pre-resume words were lost and the turn committed as a
        # fragment. Retain the pre-resume transcript here and stitch it onto
        # any post-resume transcript that doesn't already contain it
        # (merge_resumed_transcripts). Cleared once an EndOfTurn dispatches.
        self._resumed_prefix: str = ""

        # Hold-off (legacy): delay on_final after EndOfTurn when eager mode is off.
        self._eot_hold_task: asyncio.Task | None = None

        # Word-level quality accumulators — updated only on EndOfTurn events to
        # avoid double-counting partial transcripts from Update/EagerEndOfTurn.
        self._total_words: int = 0
        self._conf_sum: float = 0.0
        self._low_conf_word_count: int = 0

    def _apply_resume_stitching(self, text: str) -> str:
        """Reattach the retained pre-resume transcript when Flux restarted the
        transcript after a TurnResumed. No-op when the new transcript already
        carries the prefix (the documented cumulative behavior)."""
        if not self._resumed_prefix:
            return text
        stitched = merge_resumed_transcripts(self._resumed_prefix, text)
        if stitched != text:
            logger.info(
                "STT resumed-turn stitch | prefix=%r + text=%r -> %r",
                self._resumed_prefix, text, stitched,
            )
        return stitched

    # ── Helpers ───────────────────────────────────────────────────────────────

    def last_onset_monotonic(self) -> float | None:
        """The most recent utterance's true onset on OUR monotonic clock —
        session epoch + the first word's stream-relative .start (§Phase 3).
        None when word timings were unavailable."""
        return stream_to_monotonic(self._session_start, self._last_onset_stream_s)

    def _word_confidence(self, words: list) -> float:
        if words:
            scores = [getattr(w, "confidence", None) for w in words if getattr(w, "confidence", None) is not None]
            if scores:
                return sum(scores) / len(scores)
        return 1.0

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_open(self, _) -> None:
        logger.debug("STT WebSocket connection opened")

    def _on_close(self, _) -> None:
        logger.debug("STT WebSocket connection closed")
        # Flush any pending eager EOT text as a final result.
        if self._pending_text:
            elapsed_ms = (time.monotonic() - self._session_start) * 1000
            logger.debug(
                "STT session closed with pending text — scheduling flush | text=%r", self._pending_text
            )
            asyncio.create_task(
                self._on_final(self._pending_text, self._pending_confidence, elapsed_ms, [])
            )
            self._pending_text = ""
            self._pending_confidence = 0.0

    def _on_error(self, error) -> None:
        logger.error("STT error: %s", error)

    def _on_message(self, message: V2SocketClientResponse) -> None:
        elapsed_ms = (time.monotonic() - self._session_start) * 1000

        if isinstance(message, ListenV2Connected):
            logger.debug("STT connected | request_id=%s", message.request_id)
            return

        if isinstance(message, ListenV2FatalError):
            logger.error("STT fatal error: %s", message)
            return

        if not isinstance(message, ListenV2TurnInfo):
            logger.debug("STT unhandled message: type=%s", type(message).__name__)
            return

        event = message.event
        text = (message.transcript or "").strip()
        confidence = self._word_confidence(message.words)

        if event == "Update":
            logger.debug(
                "STT TurnInfo | event=Update | text=%r | conf=%.2f | elapsed=%.0fms",
                text, confidence, elapsed_ms,
            )
        else:
            logger.debug(
                "STT TurnInfo | event=%s | text=%r | conf=%.2f | eot_conf=%.2f | elapsed=%.0fms",
                event, text, confidence, message.end_of_turn_confidence, elapsed_ms,
            )

        if not text and event not in ("TurnResumed",) and not (
            event == "EndOfTurn" and self._resumed_prefix
        ):
            # Empty transcripts are skipped — EXCEPT an EndOfTurn while a
            # resumed-turn prefix is retained: dropping that would lose the
            # pre-resume words entirely (the stitch below flushes them).
            logger.debug("STT empty transcript — skipping")
            return

        low_conf_words = [
            w.word for w in (message.words or [])
            if getattr(w, "confidence", 1.0) < settings.stt_word_conf_threshold
        ]
        # §Phase 3: capture the utterance onset from the FIRST word's stream-
        # relative .start (seconds into the audio) at turn boundaries. Skew-free
        # — a delta in a stream we sent, anchored on our monotonic epoch.
        if event in ("EagerEndOfTurn", "EndOfTurn") and message.words:
            _first_start = getattr(message.words[0], "start", None)
            if _first_start is not None:
                self._last_onset_stream_s = float(_first_start)
        if low_conf_words:
            logger.debug("STT low-confidence words | words=%r", low_conf_words)

        if event == "EndOfTurn":
            # Resumed-turn continuity: reattach any retained pre-resume words
            # (no-op when Flux kept the transcript cumulative). The turn is
            # over — the prefix is consumed here.
            text = self._apply_resume_stitching(text)
            self._resumed_prefix = ""
            logger.info(
                "STT end-of-turn | text=%r | conf=%.2f | eager_mode=%s",
                text, confidence, self._eager_mode,
            )
            for w in (message.words or []):
                self._total_words += 1
                c = getattr(w, "confidence", 1.0)
                self._conf_sum += c
                if c < settings.stt_word_conf_threshold:
                    self._low_conf_word_count += 1
            self._pending_text = ""
            self._pending_confidence = 0.0
            self._cancel_eot_hold()
            if self._eager_mode:
                asyncio.create_task(
                    self._on_final(text, confidence, elapsed_ms, low_conf_words)
                )
            else:
                self._eot_hold_task = asyncio.create_task(
                    self._dispatch_final_after_hold(text, confidence, elapsed_ms, low_conf_words)
                )

        elif event == "EagerEndOfTurn":
            # Stitch BEFORE speculation starts, so the speculative pipeline and
            # any eager-commit fallback both see the full utterance. Pending
            # holds the stitched text so a second resume accumulates correctly.
            text = self._apply_resume_stitching(text)
            logger.info("STT eager end-of-turn | text=%r", text)
            self._pending_text = text
            self._pending_confidence = confidence
            if self._eager_mode:
                asyncio.create_task(
                    self._on_eager_end(text, confidence, elapsed_ms, low_conf_words)
                )
            else:
                asyncio.create_task(self._on_interim(text, confidence))

        elif event == "TurnResumed":
            logger.info("STT turn resumed (was eager EOT) | text=%r", text)
            # RETAIN the pre-resume transcript instead of discarding it: if
            # Flux restarts the transcript after the resume (observed live),
            # the retained prefix is stitched onto the post-resume eager/final
            # so the turn commits with the FULL utterance. Multiple resumes
            # accumulate through the same merge.
            if self._pending_text:
                self._resumed_prefix = merge_resumed_transcripts(
                    self._resumed_prefix, self._pending_text,
                )
            self._cancel_eot_hold()
            self._pending_text = ""
            self._pending_confidence = 0.0
            if self._eager_mode:
                asyncio.create_task(self._on_turn_resumed())
            elif text:
                asyncio.create_task(self._on_interim(text, confidence))

        else:
            # "Update" or "StartOfTurn" — cancel any pending hold (user still speaking)
            if event == "StartOfTurn":
                self._cancel_eot_hold()
            if text:
                asyncio.create_task(self._on_interim(text, confidence))

    def _cancel_eot_hold(self) -> None:
        if self._eot_hold_task and not self._eot_hold_task.done():
            self._eot_hold_task.cancel()
            logger.debug("STT EOT hold cancelled")
        self._eot_hold_task = None

    async def _dispatch_final_after_hold(
        self, text: str, confidence: float, elapsed_ms: float, low_conf_words: list[str]
    ) -> None:
        """Wait eot_hold_ms then fire on_final — cancelled if user resumes speaking."""
        try:
            hold_sec = settings.deepgram_eot_hold_ms / 1000.0
            await asyncio.sleep(hold_sec)
            logger.debug("STT EOT hold elapsed — dispatching final | text=%r", text)
            await self._on_final(text, confidence, elapsed_ms, low_conf_words)
        except asyncio.CancelledError:
            logger.debug("STT EOT hold cancelled — user resumed speaking | text=%r", text)

    def quality_stats(self) -> dict:
        """Return word-level transcription quality stats accumulated over the session."""
        if not self._total_words:
            return {"total_words": 0, "avg_confidence": None, "low_conf_fraction": None}
        avg = self._conf_sum / self._total_words
        low_frac = self._low_conf_word_count / self._total_words
        return {
            "total_words": self._total_words,
            "avg_confidence": round(avg, 3),
            "low_conf_fraction": round(low_frac, 3),
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> float:
        """Open Deepgram Flux v2 streaming connection. Returns elapsed ms."""
        t0 = time.monotonic()

        client = AsyncDeepgramClient(api_key=settings.deepgram_api_key)

        if self._keyterms:
            logger.debug("STT keyterms set | keyterms=%r", self._keyterms)

        connect_kwargs: dict = dict(
            model=settings.deepgram_stt_model,
            encoding=self._encoding,
            sample_rate=str(self._sample_rate),
            eot_threshold=str(self._eot_threshold),
            eot_timeout_ms=str(self._eot_timeout_ms),
        )
        if self._eager_mode and self._eager_eot_threshold is not None:
            eager = min(self._eager_eot_threshold, self._eot_threshold)
            connect_kwargs["eager_eot_threshold"] = str(eager)
        if self._keyterms:
            connect_kwargs["keyterm"] = self._keyterms

        self._ctx = client.listen.v2.connect(**connect_kwargs)
        self._connection = await self._ctx.__aenter__()

        self._connection.on(EventType.OPEN, self._on_open)
        self._connection.on(EventType.MESSAGE, self._on_message)
        self._connection.on(EventType.CLOSE, self._on_close)
        self._connection.on(EventType.ERROR, self._on_error)

        self._listen_task = asyncio.create_task(self._connection.start_listening())
        self._connected = True

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "STT session ready | model=%s | encoding=%s | sample_rate=%d | "
            "eot_threshold=%.2f | eot_timeout_ms=%d | eager_mode=%s | opened_in=%.0fms",
            settings.deepgram_stt_model,
            self._encoding,
            self._sample_rate,
            self._eot_threshold,
            self._eot_timeout_ms,
            self._eager_mode,
            elapsed_ms,
        )
        return elapsed_ms

    async def send_audio(self, chunk: bytes) -> None:
        if self._connection and self._connected:
            logger.debug("STT sending audio | bytes=%d", len(chunk))
            await self._connection._send(chunk)
        else:
            logger.warning("STT send_audio called but connection not ready")

    async def finish_utterance(self) -> None:
        """In v2, turn detection is fully model-driven — no manual finalize needed."""
        logger.debug("STT finish_utterance called (no-op in v2; EOT is model-driven)")

    async def close(self) -> None:
        self._cancel_eot_hold()
        self._connected = False
        if self._connection:
            try:
                await self._connection.send_close_stream()
            except Exception:
                pass
        if self._ctx:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        logger.debug("STT session closed")
