"""TwilioTransport.wait_playout_heard — the drain signal the transfer waits
on before redirecting the caller. Twilio echoes a stream's end mark only
after the audio has PLAYED; a call update drops anything still buffered.

    cd voice-gateway
    python3 -m pytest tests/escalation/test_playout_heard.py -q
"""
from __future__ import annotations

import asyncio
import time

from transport_fixtures import bare_twilio_transport


def test_returns_at_once_when_nothing_is_buffered():
    t = bare_twilio_transport()
    assert asyncio.run(t.wait_playout_heard(1.0)) is True


def test_waits_for_the_mark_echo():
    t = bare_twilio_transport()
    t._pending_marks["tts_end_handoff-1"] = (time.monotonic(), "Connecting you")

    async def scenario():
        async def echo():
            await asyncio.sleep(0.15)                 # Twilio finishes playing
            await t.handle_text_frame({"event": "mark",
                                       "mark": {"name": "tts_end_handoff-1"}})
        started = time.monotonic()
        _, heard = await asyncio.gather(echo(), t.wait_playout_heard(2.0))
        return heard, time.monotonic() - started

    heard, waited = asyncio.run(scenario())
    assert heard is True and 0.1 <= waited < 1.0


def test_a_mark_that_never_echoes_is_capped():
    t = bare_twilio_transport()
    t._pending_marks["tts_end_handoff-1"] = (time.monotonic(), "Connecting you")
    started = time.monotonic()
    assert asyncio.run(t.wait_playout_heard(0.2)) is False
    assert 0.2 <= time.monotonic() - started < 0.6
