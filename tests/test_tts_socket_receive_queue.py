"""The TTS socket's receive queue must be unbounded — and only the TTS one.

THE DEFECT (call 18b3f7a3, 2026-08-30). Deepgram synthesizes at ~2x realtime;
the transport's paced release hands Twilio audio at 1x. websockets' default
max_queue is 32 messages, and legacy's transfer_data() parks once the queue is
full — PONG frames are read only from inside that same parked loop, so a slow
consumer stops answering keepalive pings and the client kills its own healthy
socket:

    TTS streaming error (sentence mode): sent 1011 (internal error)
    keepalive ping timeout; no close frame received
    TTS sentence stream done | sentences=3/4 audio_ms=40920 elapsed_ms=40485

The same call's apology retry ran BURST on the same socket type, consumed at
ratio=2.18, and streamed 43.68s clean — the consumption rate is the whole
difference.

STT is deliberately NOT patched: it is a fast consumer and keeps the bounded
queue that protects it from a flood.
"""
from __future__ import annotations

import functools
import importlib

import vogon.gateway.pipeline.tts as tts_module  # importing applies the patch

_SPEAK_MODULES = ("deepgram.speak.v1.client", "deepgram.speak.v1.raw_client")
_LISTEN_MODULES = ("deepgram.listen.v2.client", "deepgram.listen.v2.raw_client")


def _connect_fn(mod_name: str):
    return importlib.import_module(mod_name).websockets_client_connect


def test_the_speak_socket_opens_with_an_unbounded_receive_queue():
    for mod_name in _SPEAK_MODULES:
        fn = _connect_fn(mod_name)
        assert isinstance(fn, functools.partial), (
            f"{mod_name} was not patched — a paced consumer can stall the "
            f"keepalive reader again")
        assert fn.keywords.get("max_queue", "missing") is None, (
            f"{mod_name} keywords={fn.keywords}")


def test_keepalive_is_left_alone():
    """Disabling ping_interval would hide a genuinely dead socket, which is the
    failure TTSClient.reconnect() exists for. Only the queue bound changes."""
    for mod_name in _SPEAK_MODULES:
        assert "ping_interval" not in _connect_fn(mod_name).keywords
        assert "ping_timeout" not in _connect_fn(mod_name).keywords


def test_the_stt_socket_is_untouched():
    """Blast radius. The SDK's own install_transport() patches all eight
    generated modules; this patch names the two speak ones so the STT socket
    keeps its bounded queue."""
    for mod_name in _LISTEN_MODULES:
        fn = _connect_fn(mod_name)
        assert not isinstance(fn, functools.partial), (
            f"{mod_name} was patched — the STT socket should keep its default "
            f"receive queue")


def test_applying_it_twice_does_not_double_wrap():
    before = _connect_fn(_SPEAK_MODULES[0])
    assert tts_module._unbound_speak_receive_queue() == 0
    assert _connect_fn(_SPEAK_MODULES[0]) is before
