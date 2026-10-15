"""Voice transport — STT input (Deepgram) + TTS output (Deepgram) with barge-in."""
from __future__ import annotations

import asyncio
import audioop
import logging
import math
import struct
import time
from collections.abc import Awaitable, Callable

from vogon.gateway.pipeline.stt import STTSession
from vogon.gateway.pipeline.tts import TTSClient
from vogon.gateway.settings import settings
from vogon.gateway.transport.base import BaseTransport
from vogon.gateway.transport.clocks import CallClocks
from vogon.gateway.transport.delivery import DeliveryReport, estimate_total_ms
from vogon_contract import UtteranceEager, UtteranceFinal, UtteranceResumed

logger = logging.getLogger(__name__)

_TTS_MERGE_BYTES = 8192
# RMS threshold for separating speech frames (signal) from silence (noise).
# Linear16 scale: ~500 ≈ -30 dBFS — above background hiss, below quiet speech.
_SPEECH_RMS_THRESHOLD = 500


def _encode_audio_frame(audio_id: str, audio_bytes: bytes) -> bytes:
    id_bytes = audio_id.encode("utf-8")
    return struct.pack("<I", len(id_bytes)) + id_bytes + audio_bytes


class VoiceTransport(BaseTransport):
    """Voice I/O: streams PCM from client into STT, streams TTS back as PCM."""

    def __init__(
        self,
        safe_send_text: Callable[[str, dict], Awaitable[None]],
        safe_send_binary: Callable[[bytes], Awaitable[None]],
        call_id: str = "",
        call_config=None,   # gateway CallConfig — the ONLY config input
    ) -> None:
        self._safe_send_text = safe_send_text
        self._safe_send_binary = safe_send_binary
        self._call_id = call_id
        self._cfg = call_config

        self._utterance_queue: asyncio.Queue = asyncio.Queue()
        self._clocks: CallClocks | None = (
            CallClocks(call_config, self._utterance_queue.put_nowait, self.send_response)
            if call_config is not None else None
        )
        self._tts = TTSClient(
            voice=(call_config.persona_voice or None)
            if call_config is not None else None,
        )
        self._stt_open_ms: float | None = None
        self._tts_playing = False
        self._tts_cancel = asyncio.Event()
        self._last_utterance_t: float = 0.0
        self._stt: STTSession | None = None
        self._barge_in_count: int = 0
        self._signal_rms_sum: float = 0.0
        self._signal_frame_count: int = 0
        self._noise_rms_sum: float = 0.0
        self._noise_frame_count: int = 0

    @property
    def utterance_queue(self) -> asyncio.Queue:
        return self._utterance_queue

    async def start(self) -> None:
        """Open the Deepgram STT session and wire STT callbacks."""

        async def on_interim(text: str, confidence: float) -> None:
            if self._tts_playing and not self._tts_cancel.is_set():
                logger.info(
                    "Barge-in detected while TTS playing — cancelling stream | text=%r",
                    text[:60],
                )
                self._barge_in_count += 1
                self._tts_cancel.set()
            await self._safe_send_text("server.transcript_interim", {
                "text": text, "confidence": confidence, "is_final": False,
            })

        async def on_eager_end(
            text: str, confidence: float, elapsed_ms: float, low_conf_words: list[str]
        ) -> None:
            await self._utterance_queue.put(
                UtteranceEager(text=text, confidence=confidence,
                               low_conf_words=low_conf_words,
                               seq=self.next_event_seq())
            )

        async def on_turn_resumed() -> None:
            await self._utterance_queue.put(
                UtteranceResumed(seq=self.next_event_seq())
            )

        async def on_final(text: str, confidence: float, elapsed_ms: float, low_conf_words: list[str]) -> None:
            self._last_utterance_t = time.monotonic()
            await self._safe_send_text("server.transcript_final", {
                "text": text, "confidence": confidence,
            })
            await self._utterance_queue.put(
                UtteranceFinal(text=text, confidence=confidence,
                               low_conf_words=low_conf_words,
                               at=self._last_utterance_t,
                               seq=self.next_event_seq())
            )

        name_hint = self._cfg.name_hint if self._cfg is not None else None
        keyterms = name_hint.split() if name_hint else []

        self._stt = STTSession(
            on_interim=on_interim,
            on_final=on_final,
            language=self._cfg.language if self._cfg is not None else "en-US",
            keyterms=keyterms,
            eager_eot_threshold=settings.deepgram_eager_eot_threshold,
            on_eager_end=on_eager_end,
            on_turn_resumed=on_turn_resumed,
        )
        # Reported as data in transport_analytics(); the transport writes
        # no analytics rows itself.
        self._stt_open_ms = await self._stt.start()

        await self._tts.open()
        if self._clocks is not None:
            self._clocks.start()

    async def close(self) -> None:
        if self._clocks is not None:
            await self._clocks.stop()
        if self._stt:
            await self._stt.close()
        await self._tts.close()

    async def send_response(
        self, text: str, audio_id: str, protected: bool = False,
        absorb_barge: bool = False,
    ) -> DeliveryReport | None:
        """Stream TTS audio to the client with barge-in detection.

        protected is accepted for interface parity and ignored: the widget's
        barge-in is client-side (browser VAD), so intro/farewell protection is
        a client concern there.

        Returns a DeliveryReport (complete/truncated/failed); None when there
        was nothing to deliver. audio_ms is None — widget chunks have no fixed
        frame duration, so only chunk counts are known.
        """
        if not text or not text.strip():
            logger.warning(
                "send_response called with empty text — skipping (audio_id=%s)", audio_id
            )
            return None
        self._tts_cancel.clear()
        self._tts_playing = True
        chunks_sent = 0
        _had_error = False
        _fc_ms: float | None = None
        _fc_t: float | None = None
        try:
            await self._safe_send_text("server.tts_stream_start", {"audio_id": audio_id})
            pending = bytearray()
            async for chunk, first_chunk_latency in self._tts.stream(text):
                if self._tts_cancel.is_set():
                    logger.debug(
                        "TTS stream interrupted (barge-in) after %d chunks — audio_id=%s",
                        chunks_sent, audio_id,
                    )
                    await self._tts.cancel()
                    await self._safe_send_text("server.tts_interrupted", {"audio_id": audio_id})
                    return DeliveryReport(
                        audio_id=audio_id,
                        status="truncated",
                        chunks_sent=chunks_sent,
                        audio_ms=None,
                        est_total_ms=estimate_total_ms(text),
                        first_chunk_ms=_fc_ms,
                        first_chunk_t=_fc_t,
                    )
                if first_chunk_latency is not None and _fc_ms is None:
                    _fc_ms = first_chunk_latency
                pending.extend(chunk)
                if len(pending) >= _TTS_MERGE_BYTES:
                    if chunks_sent == 0:
                        _fc_t = time.monotonic()
                    await self._safe_send_binary(_encode_audio_frame(audio_id, bytes(pending)))
                    chunks_sent += 1
                    pending = bytearray()
            if pending:
                if chunks_sent == 0:
                    _fc_t = time.monotonic()
                await self._safe_send_binary(_encode_audio_frame(audio_id, bytes(pending)))
                chunks_sent += 1
            await self._safe_send_text("server.tts_stream_end", {"audio_id": audio_id})
        except Exception as exc:
            logger.error("TTS streaming error: %s", exc)
            _had_error = True
            await self._safe_send_text("server.error", {
                "code": "tts_error", "message": "Voice synthesis failed", "fatal": False,
            })
        finally:
            self._tts_playing = False
        return DeliveryReport(
            audio_id=audio_id,
            status="failed" if _had_error else "complete",
            chunks_sent=chunks_sent,
            audio_ms=None,
            est_total_ms=estimate_total_ms(text),
            first_chunk_ms=_fc_ms,
            first_chunk_t=_fc_t,
        )

    async def handle_binary_frame(self, data: bytes) -> None:
        if self._stt:
            try:
                rms = audioop.rms(data, 2)
            except Exception:
                rms = 0
            if rms > _SPEECH_RMS_THRESHOLD:
                self._signal_rms_sum += rms
                self._signal_frame_count += 1
            elif rms > 0:
                self._noise_rms_sum += rms
                self._noise_frame_count += 1
            await self._stt.send_audio(data)

    async def handle_text_frame(self, msg: dict) -> bool:
        msg_type = msg.get("type", "")
        if msg_type == "client.audio_end":
            if self._stt:
                await self._stt.finish_utterance()
        elif msg_type == "client.barge_in":
            if self._tts_playing and not self._tts_cancel.is_set():
                logger.debug("Barge-in signal from client VAD — cancelling TTS")
                self._barge_in_count += 1
                self._tts_cancel.set()
        elif msg_type == "client.hangup":
            return True
        elif msg_type == "client.ready":
            logger.debug("Client ready signal received")
        return False

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
            "metadata": {"count": self._barge_in_count},
        }
        return rows
