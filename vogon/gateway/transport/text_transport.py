"""Text-only transport — no audio, typed messages only."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from vogon.gateway.transport.base import BaseTransport
from vogon.gateway.transport.clocks import CallClocks
from vogon.gateway.transport.delivery import DeliveryReport
from vogon_contract import UtteranceFinal

logger = logging.getLogger(__name__)


class TextTransport(BaseTransport):
    """Text-only transport: user sends typed messages, assistant replies with text."""

    def __init__(
        self,
        safe_send_text: Callable[[str, dict], Awaitable[None]],
        call_id: str = "",
        call_config=None,   # gateway CallConfig — the ONLY config input
    ) -> None:
        self._safe_send_text = safe_send_text
        self._call_id = call_id
        self._utterance_queue: asyncio.Queue = asyncio.Queue()
        self._clocks = (
            CallClocks(call_config, self._utterance_queue.put_nowait, self.send_response)
            if call_config is not None else None
        )

    @property
    def utterance_queue(self) -> asyncio.Queue:
        return self._utterance_queue

    async def start(self) -> None:
        if self._clocks is not None:
            self._clocks.start()

    async def close(self) -> None:
        if self._clocks is not None:
            await self._clocks.stop()

    async def send_response(
        self, text: str, audio_id: str, protected: bool = False,
        absorb_barge: bool = False,
    ) -> DeliveryReport | None:
        # protected is accepted for interface parity; text frames are atomic.
        if not text or not text.strip():
            return None
        _send_t = time.monotonic()
        await self._safe_send_text("server.message", {"text": text, "audio_id": audio_id})
        # Text delivery is atomic — a sent frame is a complete delivery. The
        # send moment doubles as first_chunk_t so handler-side latency metrics
        # work identically in text mode.
        return DeliveryReport(
            audio_id=audio_id, status="complete", chunks_sent=1, first_chunk_t=_send_t,
        )

    async def handle_binary_frame(self, data: bytes) -> None:
        pass  # no audio in text mode

    async def handle_text_frame(self, msg: dict) -> bool:
        msg_type = msg.get("type", "")
        if msg_type == "client.message":
            text = ((msg.get("payload") or {}).get("text") or "").strip()
            if text:
                # Text mode has no STT: full confidence and no low-confidence
                # words, stated explicitly (the fields are required by contract).
                await self._utterance_queue.put(
                    UtteranceFinal(text=text, confidence=1.0, low_conf_words=[],
                                   at=time.monotonic(),
                                   seq=self.next_event_seq())
                )
        elif msg_type == "client.hangup":
            return True
        return False
