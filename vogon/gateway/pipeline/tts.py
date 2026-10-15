"""Deepgram Text-to-Speech — WebSocket streaming (SDK v6).

Uses a persistent WebSocket connection per call rather than one HTTP request
per utterance. Benefits: no per-utterance TCP/TLS overhead, smaller chunk
cadence (~18ms avg vs ~69ms on REST), and clean barge-in via Clear/Cleared.

Lifecycle: call open() once at call start, close() at call end.
For barge-in: call cancel() to flush the socket before the next utterance.
"""
from __future__ import annotations

import asyncio
import functools
import importlib
import logging
import time
from collections.abc import AsyncGenerator

from deepgram import AsyncDeepgramClient
from deepgram.speak.v1.types.speak_v1cleared import SpeakV1Cleared
from deepgram.speak.v1.types.speak_v1flushed import SpeakV1Flushed
from deepgram.speak.v1.types.speak_v1metadata import SpeakV1Metadata
from deepgram.speak.v1.types.speak_v1text import SpeakV1Text

from vogon.gateway.settings import settings

logger = logging.getLogger(__name__)

_RIFF_MAGIC = b"RIFF"
# Seconds to wait for Deepgram's `Cleared` ack after a barge sends Clear. On
# timeout we log and move on, leaving whatever is still queued in a socket that
# is reused for the whole call — so this bounds how long a barge blocks the next
# utterance, at the cost of a dirtier socket when it expires.
# See docs/voice-timing-constants.md §5.
_CANCEL_DRAIN_TIMEOUT = 0.3


def _unbound_speak_receive_queue() -> int:
    """Remove the 32-message cap on the TTS socket's receive queue.

    THE DEFECT (call 18b3f7a3, 2026-08-30). Deepgram synthesizes at ~2x
    realtime; the transport's paced release hands Twilio audio at 1x. So on any
    long turn the socket delivers faster than we read it. websockets' default
    `max_queue` is 32 messages, and legacy's transfer_data() PARKS on
    `_put_message_waiter` once the queue is full (websockets/legacy/protocol.py
    :952) — while PONG frames are processed ONLY inside read_data_frame(),
    reached from that same parked loop. A slow consumer therefore stops
    answering keepalive pings, and the client kills its own healthy socket:
    ping_interval 20s + ping_timeout 20s, measured dead at elapsed_ms=40485
    with "sent 1011 (internal error) keepalive ping timeout".

    The same call carries the control: the apology retry ran BURST on the same
    socket type, consumed at ratio=2.18, and streamed 43.68s clean. Paced died,
    burst survived — the consumption rate is the whole difference.

    Scoped to the SPEAK modules on purpose. STT (deepgram.listen.v2) is a fast
    consumer and keeps the bounded queue that protects it from a flood. The
    SDK hardcodes its connect kwargs (speak/v1/client.py:212), so a wrapper on
    the module-level name is the only seam; `install_transport` is not it — it
    patches all eight generated modules and expects an AsyncTransport
    implementation rather than a websockets protocol.

    ping_interval is deliberately LEFT ALONE: disabling keepalive would hide a
    genuinely dead socket, which is the failure reconnect() exists for.

    Returns the number of modules patched (0 if already applied).
    """

    patched = 0
    for mod_name in ("deepgram.speak.v1.client", "deepgram.speak.v1.raw_client"):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:                                     # noqa: PERF203
            continue
        fn = getattr(mod, "websockets_client_connect", None)
        if fn is None or getattr(fn, "_vogon_unbounded", False):
            continue
        wrapper = functools.partial(fn, max_queue=None)
        wrapper._vogon_unbounded = True
        mod.websockets_client_connect = wrapper
        patched += 1
    return patched


_PATCHED_MODULES = _unbound_speak_receive_queue()
if _PATCHED_MODULES:
    logger.info(
        "TTS socket: receive queue unbounded on %d Deepgram speak module(s) "
        "— a paced consumer can no longer stall the keepalive reader",
        _PATCHED_MODULES,
    )


def _strip_wav_header(data: bytes) -> bytes:
    """Strip RIFF/WAV container header and return raw PCM bytes.

    WebSocket TTS does not wrap chunks in WAV containers, so this is a no-op
    in practice. Kept as a safety net against unexpected format changes.
    """
    if len(data) < 12 or data[:4] != _RIFF_MAGIC:
        return data

    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        chunk_size = int.from_bytes(data[offset + 4:offset + 8], "little")
        offset += 8
        if chunk_id == b"data":
            logger.debug(
                "Stripped WAV header (%d bytes stripped, %d bytes remain)",
                offset, len(data) - offset,
            )
            return data[offset:]
        offset += chunk_size

    logger.warning("WAV 'data' chunk not found — returning raw bytes unchanged")
    return data


class TTSClient:
    def __init__(
        self,
        voice: str | None = None,
        encoding: str = "linear16",
        sample_rate: int = 24000,
        container: str | None = None,  # kept for call-site compat; unused on WS path
    ):
        self._client = AsyncDeepgramClient(api_key=settings.deepgram_api_key)
        self._voice = voice or settings.deepgram_tts_model
        self._encoding = encoding
        self._sample_rate = sample_rate
        self._ws_client = None
        self._ws_cm = None

    async def open(self) -> None:
        """Open a persistent WebSocket TTS connection for the duration of a call."""
        self._ws_cm = self._client.speak.v1.connect(
            encoding=self._encoding,
            model=self._voice,
            sample_rate=str(self._sample_rate),
        )
        self._ws_client = await self._ws_cm.__aenter__()
        logger.info(
            "TTS WebSocket opened | voice=%s encoding=%s sample_rate=%d",
            self._voice, self._encoding, self._sample_rate,
        )

    async def close(self) -> None:
        """Close the WebSocket connection at call end."""
        if self._ws_cm:
            try:
                await self._ws_cm.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug("TTS WebSocket close error (ignored): %s", exc)
            finally:
                self._ws_client = None
                self._ws_cm = None

    async def reconnect(self) -> bool:
        """Tear the socket down and open a fresh one. Returns True on success.

        The connection is opened ONCE per call and reused for every turn, so a
        socket that dies mid-call stays dead: `stream()` sends into the corpse,
        the send raises immediately, and every retry after it fails the same
        way in about a millisecond. On call c51a075a a Deepgram keepalive ping
        timeout (1011) killed the socket 64s into one long turn, and the caller
        heard NOTHING for the rest of the call — the mid-stream apology, the
        retry, and the "are you still there?" probe all streamed 0 frames into
        the same dead connection while the logs recorded them as spoken.

        Best-effort by contract: a failed reopen must not raise into the audio
        path, because the caller of last resort is a live phone call.
        """
        try:
            await self.close()
        except Exception as exc:                                # noqa: BLE001
            logger.debug("TTS reconnect: close failed (ignored): %s", exc)
        try:
            await self.open()
            logger.info("TTS WebSocket reconnected")
            return True
        except Exception as exc:                                # noqa: BLE001
            logger.error("TTS WebSocket reconnect FAILED: %s", exc)
            self._ws_client = None
            self._ws_cm = None
            return False

    @property
    def is_open(self) -> bool:
        return self._ws_client is not None

    async def cancel(self) -> None:
        """Cancel in-flight TTS generation on barge-in.

        Sends Clear and drains the socket until Cleared is received, so the
        next stream() call starts with a clean WebSocket buffer.
        """
        if not self._ws_client:
            return
        try:
            await self._ws_client.send_clear()

            async def _drain_to_cleared() -> None:
                async for msg in self._ws_client:
                    if isinstance(msg, SpeakV1Cleared):
                        logger.debug("TTS Cleared | sequence_id=%s", msg.sequence_id)
                        return

            await asyncio.wait_for(_drain_to_cleared(), timeout=_CANCEL_DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "TTS cancel: Cleared drain timed out after %.0fms",
                _CANCEL_DRAIN_TIMEOUT * 1000,
            )
        except Exception as exc:
            logger.warning("TTS cancel error: %s", exc)

    async def stream_segments(
        self, segments: list[str],
    ) -> AsyncGenerator[tuple[int, bytes, float | None], None]:
        """Stream multi-segment TTS on ONE WebSocket (bounded playout buffer,
        full-duplex-semantic-barge-in §6.2a).

        All segments are sent upfront (text + flush each) — Deepgram queues and
        synthesizes ahead (~2x realtime; spike 2026-07-16: seg-0 first chunk
        98 ms, inter-segment gaps 59–74 ms), giving prefetch for free while the
        transport's paced sender releases at real time + lead. Each segment's
        Flushed event demarcates its audio.

        Yields (segment_idx, chunk_bytes, first_chunk_latency_ms) — latency set
        only on the stream's very first audio chunk. Stops after the LAST
        segment's Flushed.
        """
        if not self._ws_client:
            raise RuntimeError("TTSClient.open() must be called before stream_segments()")
        if not segments:
            return

        t0 = time.monotonic()
        first_chunk_seen = False
        seg = 0
        flushed = 0

        for s in segments:
            await self._ws_client.send_text(SpeakV1Text(text=s))
            await self._ws_client.send_flush()

        async for message in self._ws_client:
            if isinstance(message, SpeakV1Flushed):
                flushed += 1
                logger.debug(
                    "TTS Flushed | seg=%d/%d sequence_id=%s",
                    flushed, len(segments), message.sequence_id,
                )
                seg = flushed
                if flushed >= len(segments):
                    break
                continue
            if isinstance(message, SpeakV1Metadata):
                continue
            if not isinstance(message, bytes):
                continue
            chunk = _strip_wav_header(message)
            if not chunk:
                continue
            latency = None
            if not first_chunk_seen:
                first_chunk_seen = True
                latency = (time.monotonic() - t0) * 1000
            yield seg, chunk, latency

        logger.info(
            "TTS segments complete | segments=%d flushed=%d total_ms=%.0f",
            len(segments), flushed, (time.monotonic() - t0) * 1000,
        )

    async def stream(self, text: str) -> AsyncGenerator[tuple[bytes, float | None], None]:
        """Stream TTS audio via WebSocket.

        Yields (chunk_bytes, first_chunk_latency_ms) for the first audio chunk
        and (chunk_bytes, None) for all subsequent chunks. Stops at Flushed.

        Raises RuntimeError if open() has not been called.
        """
        if not self._ws_client:
            raise RuntimeError("TTSClient.open() must be called before stream()")

        t0 = time.monotonic()
        chunk_num = 0
        total_bytes = 0
        _last_chunk_t: float | None = None

        logger.debug(
            "TTS stream request | voice=%s text_len=%d preview=%r",
            self._voice, len(text), text[:80],
        )

        await self._ws_client.send_text(SpeakV1Text(text=text))
        await self._ws_client.send_flush()

        async for message in self._ws_client:
            now = time.monotonic()

            if isinstance(message, SpeakV1Flushed):
                logger.debug("TTS Flushed | sequence_id=%s", message.sequence_id)
                break

            if isinstance(message, SpeakV1Metadata):
                logger.debug(
                    "TTS Metadata | request_id=%s model=%s",
                    message.request_id, message.model_name,
                )
                continue

            if not isinstance(message, bytes):
                logger.debug("TTS control message | type=%s", type(message).__name__)
                continue

            chunk = _strip_wav_header(message)
            if not chunk:
                continue

            chunk_num += 1
            total_bytes += len(chunk)
            elapsed_ms = (now - t0) * 1000
            inter_chunk_ms = (now - _last_chunk_t) * 1000 if _last_chunk_t is not None else 0.0
            _last_chunk_t = now

            logger.debug(
                "TTS chunk | chunk=%d raw_bytes=%d pcm_bytes=%d elapsed_ms=%.0f inter_chunk_ms=%.0f",
                chunk_num, len(message), len(chunk), elapsed_ms, inter_chunk_ms,
            )

            yield chunk, elapsed_ms if chunk_num == 1 else None

        total_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "TTS stream complete | chunks=%d total_bytes=%d total_ms=%.0f "
            "avg_chunk_bytes=%d avg_chunk_ms=%.0f",
            chunk_num, total_bytes, total_ms,
            total_bytes // chunk_num if chunk_num else 0,
            total_ms / chunk_num if chunk_num else 0,
        )
