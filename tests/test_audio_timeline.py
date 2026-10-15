"""Audio timeline pipeline — event log + builder contract (docs/audio-timeline.md).

Drives the Twilio transport with synthetic frames + a real AudioRecorder to
produce all three per-call artifacts, then builds the timeline page from the
directory exactly as the route / offline CLI do. No network, no browser — the
page's rendering is asserted structurally (data + markers embedded).
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import json
import struct

import pytest

from vogon.gateway.audio.audio_recorder import AudioRecorder
from vogon.gateway.audio.audio_timeline import build_timeline_from_dir
from vogon.gateway.settings import settings
from vogon.gateway.transport.audio_event_log import AudioEventLog
from vogon.gateway.transport.twilio_transport import TwilioTransport


def _media(v: int) -> dict:
    mulaw = audioop.lin2ulaw(struct.pack("<160h", *([v] * 160)), 2)
    return {"event": "media", "media": {"payload": base64.b64encode(mulaw).decode()}}


class _Stt:
    async def send_audio(self, b): pass
    async def close(self): pass


class _AecPass:
    def process(self, b): return b
    def feed_reference(self, b): pass
    def discard_reference(self): pass


class _Session:
    config = {"assistant": {"workflow_id": "wf"}}
    call_id = "tl-test-call"

    def record_analytics(self, *a, **k): pass


def test_event_log_rebases_to_t0(tmp_path):
    log = AudioEventLog()
    log.emit(100.0, "tts_start", audio_id="a1")
    log.emit(101.5, "barge_confirmed", clean_rms=2000)
    log.flush(tmp_path / "x.audio_events.json", t0=99.0, meta={"k": 1})
    payload = json.loads((tmp_path / "x.audio_events.json").read_text())
    assert payload["meta"] == {"k": 1}
    assert payload["events"][0] == {"t": 1.0, "event": "tts_start", "audio_id": "a1"}
    assert payload["events"][1]["t"] == 2.5


def test_full_pipeline_artifacts_to_timeline_html(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "barge_in_frame_log", True)
    monkeypatch.setattr(settings, "recordings_path", str(tmp_path))

    async def _noop(*a, **k): pass

    t = TwilioTransport(ws=None, send_raw=_noop, call_id="tl-test-call")
    t._stt = _Stt()
    t._aec = _AecPass()
    t._stream_sid = "MZ"
    t._recorder = AudioRecorder()

    async def drive():
        # Agent speaking: quiet residual, then a >threshold burst long enough to
        # confirm a barge (threshold 1200, debounce 3, grace 100 ms — send a
        # frame first so the grace window has an anchor in the past).
        t._tts_playing = True
        t._tts_started_at = 0.0  # far in the past → grace elapsed
        t._audio_log.emit(0.0, "tts_start", audio_id="a1", text="hello there")
        # 2-frame burst dies (debounce=3 → SUPPRESSED), then 3 frames CONFIRM.
        for v in (200, 5000, 5000, 200, 5000, 5000, 5000):
            await t.handle_text_frame(_media(v))
        t._audio_log.emit(0.2, "tts_end", audio_id="a1", cancelled=True)
        t._tts_playing = False
        t._tts_ended_at = 1.0
        for v in (150, 4000, 150):
            await t.handle_text_frame(_media(v))
        await t.close()

    asyncio.run(drive())

    # All three artifacts + the recorder WAV exist.
    events = json.loads((tmp_path / "tl-test-call.audio_events.json").read_text())
    kinds = [e["event"] for e in events["events"]]
    assert "barge_streak_start" in kinds
    assert "barge_suppressed" in kinds, kinds   # the 2-frame burst the debounce killed
    assert "barge_confirmed" in kinds, kinds    # the 3-frame burst that latched
    assert events["meta"]["barge_in_rms_threshold"] == settings.barge_in_rms_threshold
    # Recorder wav: transport recorded caller chunks; write it as the route expects.
    wav_bytes = t._recorder.to_wav_bytes()
    assert wav_bytes
    (tmp_path / "tl-test-call.wav").write_bytes(wav_bytes)

    html = build_timeline_from_dir(tmp_path, "tl-test-call")
    assert "barge_confirmed" in html and "frames" in html
    assert "data:audio/wav;base64," in html
    # Embedded data round-trips: the page carries the frame rows + events.
    assert "__DATA_JSON__" not in html and "__AUDIO_B64__" not in html
    assert '"clean_rms": 4' not in html  # spot check: no truncation artifacts


def test_timeline_missing_wav_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_timeline_from_dir(tmp_path, "nope")
