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
import inspect

from vogon.gateway.pipeline.tts import TTSClient
from vogon.gateway.transport import twilio_transport


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

    def test_a_dead_socket_is_replaced_not_reused(self):
        c = _client()
        dead = c._ws_client

        async def fake_close():
            c._ws_client = None

        async def fake_open():
            c._ws_client = object()

        c.close, c.open = fake_close, fake_open
        asyncio.run(c.reconnect())
        assert c._ws_client is not dead, "retried into the same socket"


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
    retry itself streaming into the dead socket — which is the bug."""

    def _src(self) -> str:
        return inspect.getsource(twilio_transport)

    def test_reconnect_precedes_both_retry_paths(self):
        # Anchored on the logger FORMAT strings, not on prose: the comment
        # above the reconnect quotes the log lines, and searching for the
        # quoted phrases found the comment instead of the call site.
        src = self._src()
        at_reconnect = src.index("await self._tts.reconnect()")
        assert at_reconnect < src.index('"TTS stream sent 0 chunks')
        assert at_reconnect < src.index('"TTS mid-stream disconnect after %d')

    def test_a_failed_reconnect_skips_the_retry(self):
        # Retrying into a socket we just failed to reopen is the millisecond
        # no-op from the incident.
        src = self._src()
        assert "not retrying into a dead socket" in src
