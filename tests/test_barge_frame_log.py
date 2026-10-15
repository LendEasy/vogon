"""Phase 0 barge-in measurement harness — frame logger contract.

Pins the flag-gated per-frame logger in TwilioTransport (docs/barge-in-tuning.md):
  - flag ON  → close() writes <call_id>.frames.csv + <call_id>.clean.wav, one
    row/chunk per media frame, with the gated window carrying the AEC-cleaned
    audio and the ungated window carrying raw (clean == raw).
  - flag OFF → nothing is collected, nothing is written (zero production cost).

No network, no Deepgram — fake STT, passthrough AEC, synthetic μ-law frames.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import csv
import struct
import wave

from vogon.gateway.settings import settings
from vogon.gateway.transport.twilio_transport import TwilioTransport


def _media(v: int) -> dict:
    """One Twilio media event: 160 samples of constant linear16 amplitude v."""
    mulaw = audioop.lin2ulaw(struct.pack("<160h", *([v] * 160)), 2)
    return {"event": "media", "media": {"payload": base64.b64encode(mulaw).decode()}}


class _Stt:
    async def send_audio(self, b):
        pass

    async def close(self):
        pass


class _AecPass:
    def process(self, b):
        return b

    def feed_reference(self, b):
        pass

    def discard_reference(self):
        pass


class _Session:
    config = {"assistant": {"workflow_id": "wf"}}
    call_id = "test-call-framelog"

    def record_analytics(self, *a, **k):
        pass


def _make_transport():
    async def _noop(*a, **k):
        pass

    t = TwilioTransport(ws=None, send_raw=_noop, call_id="test-call-framelog")
    t._stt = _Stt()
    t._aec = _AecPass()
    t._stream_sid = "MZtest"
    t._recorder = None
    return t


def test_frame_log_writes_csv_and_wav(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "barge_in_frame_log", True)
    monkeypatch.setattr(settings, "recordings_path", str(tmp_path))
    t = _make_transport()

    async def drive():
        # 3 frames during the echo-gated window (TTS playing) …
        t._tts_playing = True
        for v in (200, 5000, 200):
            await t.handle_text_frame(_media(v))
        # … then 2 frames outside it.
        t._tts_playing = False
        t._tts_ended_at = 1.0  # long past the echo cooldown
        for v in (150, 4000):
            await t.handle_text_frame(_media(v))
        await t.close()

    asyncio.run(drive())

    csv_path = tmp_path / "test-call-framelog.frames.csv"
    wav_path = tmp_path / "test-call-framelog.clean.wav"
    assert csv_path.exists() and wav_path.exists()

    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 5
    assert [r["tts_playing"] for r in rows] == ["1", "1", "1", "0", "0"]
    # Live gate view (Phase A observability): every row carries the active
    # threshold + noise-floor estimate so the EMA trajectory is analyzable.
    assert rows[0]["threshold"] == str(settings.barge_in_rms_threshold)
    assert "noise_floor" in rows[0]
    # μ-law round-trip is lossy; RMS should land near the source amplitude.
    assert abs(int(rows[1]["clean_rms"]) - 5000) < 300
    # Ungated window: clean == raw by construction.
    assert rows[4]["clean_rms"] == rows[4]["raw_rms"]

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == 8000 and wf.getnchannels() == 1
        assert wf.getnframes() == 5 * 160  # one 20 ms frame per media event


def test_frame_log_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "barge_in_frame_log", False)
    monkeypatch.setattr(settings, "recordings_path", str(tmp_path))
    t = _make_transport()
    assert t._frame_log is None

    async def drive():
        t._tts_playing = True
        await t.handle_text_frame(_media(3000))
        await t.close()

    asyncio.run(drive())
    # The Phase 0 frame artifacts are flag-gated and must be absent. The sparse
    # audio_events.json is ALWAYS-ON by design (docs/audio-timeline.md) and may
    # legitimately exist — it is not part of this flag's contract.
    leftovers = [p.name for p in tmp_path.iterdir() if not p.name.endswith(".audio_events.json")]
    assert not leftovers, f"flag off must write no frame artifacts, got {leftovers}"
