"""A dead TTS socket must be replaced, not retried into.

CALL c51a075a, 2026-08-06. A 126-word turn streamed for 64 seconds; a Deepgram
keepalive ping timeout (1011) then killed the WebSocket with 4 of 6 sentences
delivered. Everything after that failed in about a millisecond:

    TTS mid-stream disconnect after 3256 chunks — re-speaking with apology
    TTS stream done | frames=0 audio_ms=0 elapsed_ms=1 error=True
    ...
    TTS stream sent 0 chunks (audio_id=silence-probe-7), retrying once
    TTS stream done | frames=0 audio_ms=0 elapsed_ms=0 error=True

The apology, its retry, and the "are you still there?" probe were all streamed
into the same corpse. The logs recorded them as spoken; the caller heard
nothing for the rest of the call, and the silence ladder kept probing a person
who could not hear the probe.

The connection is opened ONCE per call and reused for every turn, so nothing
ever replaced it. These tests hold the two halves of the fix: reconnect()
really does tear down and reopen, and it fails soft — the caller of last
resort is a live phone call, so a failed reopen may not raise into the audio
path.

    cd voice-gateway
    python -m pytest tests/test_tts_reconnect.py -v
"""
from __future__ import annotations

import asyncio

from transport_fixtures import bare_twilio_transport

from vogon.gateway.pipeline.tts import TTSClient


def _client() -> TTSClient:
    c = TTSClient.__new__(TTSClient)          # no network in __init__
    c._ws_client = object()
    c._ws_cm = None
    c._voice, c._encoding, c._sample_rate = "aura-2", "mulaw", 8000
    return c


class TestReconnectReplacesTheSocket:
    def test_it_closes_then_opens(self):
        c, order = _client(), []

        async def fake_close():
            order.append("close")
            c._ws_client = None

        async def fake_open():
            order.append("open")
            c._ws_client = object()

        c.close, c.open = fake_close, fake_open
        assert asyncio.run(c.reconnect()) is True
        assert order == ["close", "open"], order
        assert c.is_open

class TestItFailsSoft:
    """A live phone call is downstream of this. An exception here would take
    the call down harder than the silence it is trying to fix."""

    def test_a_failed_reopen_returns_false_and_does_not_raise(self):
        c = _client()

        async def fake_close():
            c._ws_client = None

        async def fake_open():
            raise ConnectionError("deepgram unreachable")

        c.close, c.open = fake_close, fake_open
        assert asyncio.run(c.reconnect()) is False
        assert not c.is_open, "must not report open after a failed reopen"

    def test_a_failing_close_does_not_stop_the_reopen(self):
        # The socket died on us; closing it may well throw. That must not be
        # what prevents a fresh one.
        c = _client()

        async def fake_close():
            raise RuntimeError("already closed")

        async def fake_open():
            c._ws_client = object()

        c.close, c.open = fake_close, fake_open
        assert asyncio.run(c.reconnect()) is True
        assert c.is_open


class TestTheTransportReconnectsBeforeRetrying:
    """Placement is the fix. Reconnecting AFTER the retry would leave the
    retry itself streaming into the dead socket — which is the bug. Driven
    through the real send path (_send_response) with the stream stubbed: a
    first attempt that errors with nothing sent, then whatever comes next.
    Until 2026-10-30 these compared positions of strings in the source, which
    a reconnect moved after the retry inside the same block still satisfied."""

    TEXT = "Hello there. How are you?"

    def _send(self, reopen: bool):
        order = []
        t = bare_twilio_transport(_stream_sid="S1")
        results = iter([(0, True, 2, 0, ""), (12, False, 2, 2, self.TEXT)])

        async def stream_once(text, audio_id):
            order.append("stream")
            return next(results)

        async def reconnect():
            order.append("reconnect")
            return reopen

        t._stream_once = stream_once
        t._tts.reconnect = reconnect
        return order, asyncio.run(t._send_response(self.TEXT, "a1"))

    def test_the_retry_streams_into_the_reopened_socket(self):
        order, report = self._send(reopen=True)
        assert order == ["stream", "reconnect", "stream"], order
        assert report.status == "complete" and report.chunks_sent == 12

    def test_a_failed_reconnect_skips_the_retry(self):
        # Retrying into a socket we just failed to reopen is the millisecond
        # no-op from the incident.
        order, report = self._send(reopen=False)
        assert order == ["stream", "reconnect"], order
        assert report.status == "failed" and report.chunks_sent == 0
