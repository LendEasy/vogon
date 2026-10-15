"""LiveTransport — the Twilio transport with GPT-Live 1 as its listener.

docs/GPT_LIVE_TURN_API_DESIGN.md §5, recite-only milestone. A parallel path
that changes nothing about how the call is driven: it IS TwilioTransport —
Twilio framing, AEC, the barge gate and floor controller, Deepgram TTS with
per-sentence marks, the silence ladder, holds, analytics — with exactly two
seams overridden:

  _make_stt()     the listener is a LiveSession (gpt-live-1, client
                  delegation) instead of Deepgram Flux; it emits the same
                  on_interim / on_final callbacks, so the utterance_queue,
                  the Turn API request and every barge verdict are unchanged
  send_response() after every playout, tell the live model what the caller
                  heard, so its picture of the conversation is two-sided

Selected per call by the opaque app_payload {"voice": "live"} the data plane
sets from the demo's use_live flag (handler._build_transport).
"""
from __future__ import annotations

import logging

from vogon.gateway.pipeline.live_session import LiveSession
from vogon.gateway.transport.delivery import DeliveryReport
from vogon.gateway.transport.twilio_transport import TwilioTransport

logger = logging.getLogger(__name__)


class LiveTransport(TwilioTransport):
    """Twilio media stream in and out; gpt-live-1 listening; agent lines
    spoken by Deepgram TTS as on the classic path."""

    def _make_stt(self, **kw):
        return LiveSession(
            on_interim=kw["on_interim"],
            on_final=kw["on_final"],
            language=kw.get("language", "en-US"),
            keyterms=kw.get("keyterms"),
            encoding=kw.get("encoding", "mulaw"),
            sample_rate=kw.get("sample_rate", 8000),
        )

    async def send_response(
        self, text: str, audio_id: str, protected: bool = False, absorb_barge: bool = False,
    ) -> DeliveryReport | None:
        dr = await super().send_response(text, audio_id, protected, absorb_barge)
        stt = self._stt
        if dr is not None and stt is not None and hasattr(stt, "note_agent_said"):
            heard = getattr(dr, "heard_text", "") or (text if dr.status == "complete" else "")
            if heard:
                await stt.note_agent_said(heard)
        return dr
