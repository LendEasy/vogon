"""Offline characterization of the current transport, independent of barge_sim."""
# ruff: noqa: E402 — its imports follow the sys.path setup
import asyncio
import audioop
import base64
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from transport_fixtures import bare_twilio_transport

from vogon.gateway.transport import twilio_transport as tt
from vogon.gateway.transport.barge_gate import BargeGate, BargeGateConfig


def mulaw(level):
    """20 ms, 8 kHz. Controlled energy, not simulated human speech."""
    return audioop.lin2ulaw(struct.pack('<160h', *([level] * 160)), 2)


class Clock:
    value = 100.0

    def monotonic(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds
        await asyncio.sleep(0)


class STT:
    def __init__(self, **callbacks):
        self.callbacks = callbacks
        self.send_audio = AsyncMock()
        self.start = AsyncMock(return_value=0)
        self.close = AsyncMock()

    def last_onset_monotonic(self):
        return None

    async def interim(self, text):
        await self.callbacks['on_interim'](text, 0.99)

    async def final(self, text):
        await self.callbacks['on_final'](text, 0.99, 200, [])


class Harness:
    mulaw = staticmethod(mulaw)

    def __init__(self, t, clock):
        self.t, self.clock = t, clock

    async def frame(self, raw=3000, clean=None, advance=0.02):
        self.clock.value += advance
        if clean is not None:
            self.t._aec.process.return_value = mulaw(clean)
        return await self.t.handle_text_frame({
            'event': 'media', 'media': {
                'payload': base64.b64encode(mulaw(raw)).decode(),
                'timestamp': str(round((self.clock.value - 100) * 1000)),
            },
        })

    async def mark(self, name):
        await self.t.handle_text_frame({'event': 'mark', 'mark': {'name': name}})

    def names(self):
        return [call.args[1] for call in self.t._audio_log.emit.call_args_list]

    def events(self):
        result = []
        while not self.t._utterance_queue.empty():
            result.append(self.t._utterance_queue.get_nowait())
        return result


@pytest.fixture
def baseline(monkeypatch):
    # Pin the experiment; never inherit deployment/env tuning or credentials.
    for key, value in dict(
        stt_feed_mode='open', semantic_barge_enabled=True,
        barge_controller_enabled=False,
        semantic_barge_window_ms=900, semantic_barge_llm_timeout_ms=700,
        semantic_barge_backchannel_max_tokens=3, semantic_barge_substantive_min_tokens=6,
        tts_pacing_mode='sentence', tts_buffer_lead_ms=500, barge_in_rms_threshold=1200,
        barge_in_consecutive_frames=3, barge_in_grace_ms=100,
        barge_in_adaptive_enabled=False, barge_in_confirm='off',
        barge_in_frame_log=False,
    ).items():
        monkeypatch.setattr(tt.settings, key, value)
    clock = Clock()
    monkeypatch.setattr(tt, 'time', SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(tt, 'asyncio', SimpleNamespace(
        **{name: getattr(asyncio, name) for name in dir(asyncio) if name != 'sleep'},
        sleep=clock.sleep,
    ))
    # Even accidental classifier requests cannot reach a provider.
    monkeypatch.setattr(tt, 'classify_barge', Mock(return_value=None))
    monkeypatch.setattr(tt.metrics_sink, 'emit', Mock())
    monkeypatch.setattr(tt, 'STTSession', STT)
    t = bare_twilio_transport(
        _aec=Mock(process=Mock(return_value=mulaw(3000))), _tts_encoding='mulaw',
        _audio_log=Mock(), _stream_sid='MZ-baseline',
        _pre_read_start={'streamSid': 'MZ-baseline'},
        _tts_playing=True, _tts_started_at=99.0, _tts_ended_at=0.0,
        _last_audio_id='response-1',
        _last_speak_text='Your account has an outstanding balance. Can we discuss your options?',
        _barge_gate=BargeGate(BargeGateConfig()),
    )
    t._tts.open = AsyncMock()
    t._barge_gate.on_tts_start(99.0)
    return Harness(t, clock)
