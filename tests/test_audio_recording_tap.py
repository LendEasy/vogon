"""Unit tests for TwilioTransport audio recording taps.

Verifies that AudioRecorder.add_caller_chunk and add_agent_chunk are called
at the correct points in the inbound and outbound audio paths — including that
the inbound tap fires BEFORE the echo suppression gate (so suppressed audio
still lands in the recording).

Uses TwilioTransport.__new__() to bypass the real __init__ (which would open
STT/TTS connections), then wires in mocks for all required attributes.

Run:
    cd voice-gateway
    python -m pytest tests/test_audio_recording_tap.py -v
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from transport_fixtures import bare_twilio_transport

from vogon.gateway.audio.audio_recorder import AudioRecorder
from vogon.gateway.transport.twilio_transport import TwilioTransport

# ── Helpers ───────────────────────────────────────────────────────────────────

def _mulaw_bytes(n: int = 160) -> bytes:
    return bytes([0x7F] * n)


def _media_msg(mulaw: bytes, timestamp_ms: int | None = 0) -> dict:
    """A Twilio media frame. Real frames always carry `timestamp` (ms since
    stream start); the recorder now positions the frame by it rather than by
    arrival time, so the fixture carries it too."""
    media = {"payload": base64.b64encode(mulaw).decode("ascii")}
    if timestamp_ms is not None:
        media["timestamp"] = str(timestamp_ms)
    return {"event": "media", "media": media}


def _make_transport(recorder=None, tts_playing=False) -> TwilioTransport:
    """Construct a TwilioTransport with all internals mocked out."""
    return bare_twilio_transport(
        _recorder=recorder,
        _tts_playing=tts_playing,
        _tts_ended_at=0.0,         # effectively expired — echo cooldown not active
        _stream_sid="MZ_test",
    )


# ── Construction ──────────────────────────────────────────────────────────────

class TestConstruction:
    def test_recorder_stored_when_provided(self):
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        assert t._recorder is rec

    def test_recorder_is_none_by_default(self):
        t = _make_transport(recorder=None)
        assert t._recorder is None

    def test_no_crash_without_recorder(self):
        t = _make_transport(recorder=None)
        asyncio.run(t.handle_text_frame(_media_msg(_mulaw_bytes())))
        # should not raise


# ── Inbound tap ───────────────────────────────────────────────────────────────

class TestInboundTap:
    def test_add_caller_chunk_called_on_media_event(self):
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        mulaw = _mulaw_bytes(160)
        asyncio.run(t.handle_text_frame(_media_msg(mulaw)))
        rec.add_caller_chunk.assert_called_once()
        _, actual_bytes = rec.add_caller_chunk.call_args[0]
        assert actual_bytes == mulaw

    def test_inbound_bytes_match_decoded_payload(self):
        """The bytes passed to the recorder are the raw mulaw after base64 decode."""
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        mulaw = bytes(range(160))
        asyncio.run(t.handle_text_frame(_media_msg(mulaw)))
        _, actual = rec.add_caller_chunk.call_args[0]
        assert actual == mulaw

    def test_inbound_tap_fires_before_echo_gate(self):
        """Recorder is called even when TTS is playing (echo suppression active)."""
        rec = MagicMock()
        t = _make_transport(recorder=rec, tts_playing=True)
        mulaw = _mulaw_bytes(160)
        asyncio.run(t.handle_text_frame(_media_msg(mulaw)))
        rec.add_caller_chunk.assert_called_once()
        # STT should NOT have received audio (echo gate suppressed it).
        t._stt.send_audio.assert_not_called()

    def test_inbound_tap_fires_during_echo_cooldown(self):
        """Recorder is called even during the post-TTS echo cooldown window."""
        rec = MagicMock()
        t = _make_transport(recorder=rec, tts_playing=False)
        # tts ended just now → cooldown active
        t._tts_ended_at = time.monotonic()
        asyncio.run(t.handle_text_frame(_media_msg(_mulaw_bytes(160))))
        rec.add_caller_chunk.assert_called_once()
        t._stt.send_audio.assert_not_called()

    def test_inbound_tap_not_called_on_non_media_event(self):
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        asyncio.run(t.handle_text_frame({"event": "mark", "mark": {"name": "x"}}))
        rec.add_caller_chunk.assert_not_called()

    def test_inbound_tap_not_called_when_payload_missing(self):
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        asyncio.run(t.handle_text_frame({"event": "media", "media": {}}))
        rec.add_caller_chunk.assert_not_called()

    def test_inbound_frame_is_positioned_by_the_stream_clock(self):
        """CONTRACT CHANGE: the transport no longer stamps the frame with its
        own arrival time — it asks the recorder where the frame belongs, so a
        jittered arrival cannot displace audio inside the file."""
        rec = MagicMock(wraps=AudioRecorder())
        rec.caller_timestamp.side_effect = AudioRecorder().caller_timestamp
        t = _make_transport(recorder=rec)
        asyncio.run(t.handle_text_frame(_media_msg(_mulaw_bytes(160), timestamp_ms=0)))
        asyncio.run(t.handle_text_frame(_media_msg(_mulaw_bytes(160), timestamp_ms=20)))

        # the media dict (with its timestamp) is what decides placement
        media_arg, arrival_arg = rec.caller_timestamp.call_args[0]
        assert media_arg["timestamp"] == "20"
        assert isinstance(arrival_arg, float) and arrival_arg > 0

        stamps = [c[0][0] for c in rec.add_caller_chunk.call_args_list]
        assert all(isinstance(x, float) for x in stamps)
        # exactly one frame period apart, whatever the arrival times were
        assert stamps[1] - stamps[0] == pytest.approx(0.020, abs=1e-9)

    def test_multiple_inbound_frames_all_tapped(self):
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        for _ in range(5):
            asyncio.run(t.handle_text_frame(_media_msg(_mulaw_bytes(160))))
        assert rec.add_caller_chunk.call_count == 5


# ── Outbound tap ──────────────────────────────────────────────────────────────

class TestOutboundTap:
    def test_add_agent_chunk_called_on_send_media_frame(self):
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        mulaw = _mulaw_bytes(160)
        asyncio.run(t._send_media_frame(mulaw))
        rec.add_agent_chunk.assert_called_once()
        _, actual = rec.add_agent_chunk.call_args[0]
        assert actual == mulaw

    def test_outbound_bytes_match_what_twilio_receives(self):
        """The bytes tapped to the recorder are exactly what gets base64-encoded to Twilio."""
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        mulaw = bytes(range(160))
        asyncio.run(t._send_media_frame(mulaw))

        _, tapped_bytes = rec.add_agent_chunk.call_args[0]
        assert tapped_bytes == mulaw

        # Also verify what Twilio received is the base64 of the same bytes.
        sent_json = t._send_raw.call_args[0][0]
        msg = json.loads(sent_json)
        twilio_bytes = base64.b64decode(msg["media"]["payload"])
        assert twilio_bytes == mulaw

    def test_outbound_timestamp_is_float(self):
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        asyncio.run(t._send_media_frame(_mulaw_bytes(160)))
        ts, _ = rec.add_agent_chunk.call_args[0]
        assert isinstance(ts, float)
        assert ts > 0

    def test_outbound_tap_not_called_when_recorder_none(self):
        t = _make_transport(recorder=None)
        # Should not raise AttributeError.
        asyncio.run(t._send_media_frame(_mulaw_bytes(160)))

    def test_multiple_outbound_frames_all_tapped(self):
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        for _ in range(4):
            asyncio.run(t._send_media_frame(_mulaw_bytes(160)))
        assert rec.add_agent_chunk.call_count == 4

    def test_send_raw_still_called_when_recorder_present(self):
        """Recording tap must not prevent audio reaching Twilio."""
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        asyncio.run(t._send_media_frame(_mulaw_bytes(160)))
        t._send_raw.assert_called_once()

    def test_inbound_tap_does_not_call_add_agent_chunk(self):
        """An inbound media frame must not be mistakenly tapped as agent audio."""
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        asyncio.run(t.handle_text_frame(_media_msg(_mulaw_bytes(160))))
        rec.add_agent_chunk.assert_not_called()

    def test_outbound_tap_does_not_call_add_caller_chunk(self):
        """An outbound frame must not be mistakenly tapped as caller audio."""
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        asyncio.run(t._send_media_frame(_mulaw_bytes(160)))
        rec.add_caller_chunk.assert_not_called()

    def test_consecutive_frames_have_sequential_timestamps(self):
        """Each 160-byte frame must be spaced exactly 20ms apart.

        This is the key regression test for the burst-compression bug: Deepgram
        TTS delivers audio faster than real-time, so wall-clock timestamps on
        consecutive frames are microseconds apart (causing them all to land at
        the same sample offset and overwrite each other).  The byte-cursor fix
        must produce timestamps that differ by exactly len(frame)/8000 seconds.
        """
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        frame = _mulaw_bytes(160)   # 160 bytes = 20ms at 8kHz mulaw
        # Send three frames back-to-back (simulating a TTS burst).
        asyncio.run(t._send_media_frame(frame))
        asyncio.run(t._send_media_frame(frame))
        asyncio.run(t._send_media_frame(frame))
        assert rec.add_agent_chunk.call_count == 3
        ts0, _ = rec.add_agent_chunk.call_args_list[0][0]
        ts1, _ = rec.add_agent_chunk.call_args_list[1][0]
        ts2, _ = rec.add_agent_chunk.call_args_list[2][0]
        expected_gap = 160 / 8000.0   # 0.02s = 20ms
        assert abs((ts1 - ts0) - expected_gap) < 1e-9, \
            f"Frame 1→2 gap {ts1-ts0:.6f}s should be {expected_gap:.6f}s"
        assert abs((ts2 - ts1) - expected_gap) < 1e-9, \
            f"Frame 2→3 gap {ts2-ts1:.6f}s should be {expected_gap:.6f}s"

    def test_byte_cursor_resets_between_utterances(self):
        """_do_tts_stream resets the byte cursor, so each utterance anchors fresh."""
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        # Simulate first utterance: send one frame, cursor advances.
        asyncio.run(t._send_media_frame(_mulaw_bytes(160)))
        ts_first, _ = rec.add_agent_chunk.call_args_list[0][0]
        # Simulate _do_tts_stream reset between utterances.
        t._rec_tts_start_ts = 0.0
        t._rec_tts_byte_offset = 0
        # Second utterance first frame.
        asyncio.run(t._send_media_frame(_mulaw_bytes(160)))
        ts_second, _ = rec.add_agent_chunk.call_args_list[1][0]
        # Second utterance gets a fresh start_ts (>= first utterance's start_ts).
        assert ts_second >= ts_first


# ── Stream epoch wiring ───────────────────────────────────────────────────────

class TestStreamEpochWiring:
    """The recorder's timeline origin is the moment the stream began, and only
    the transport knows that moment — it is the "start" event. If this wiring
    is missing the recorder falls back to deriving the origin from traffic,
    which is order-dependent: whichever leg is captured first claims it.
    """

    @staticmethod
    def _drive_start(transport) -> None:
        start_msg = {
            "event": "start",
            "streamSid": "MZ_test",
            "start": {"streamSid": "MZ_test", "callSid": "CA_test"},
        }
        transport._ws.receive = AsyncMock(return_value={"text": json.dumps(start_msg)})
        asyncio.run(transport._wait_for_stream_start())

    def test_start_event_hands_the_stream_epoch_to_the_recorder(self):
        rec = MagicMock()
        t = _make_transport(recorder=rec)
        self._drive_start(t)
        assert t._stream_sid == "MZ_test", "the start event was actually processed"
        rec.set_stream_epoch.assert_called_once()
        (epoch,), _ = rec.set_stream_epoch.call_args
        assert isinstance(epoch, float)
        # Must be OUR monotonic clock — the recorder places the agent leg on it
        # too, and two legs in different time domains cannot be interleaved.
        assert abs(epoch - time.monotonic()) < 1.0

    def test_epoch_reaches_a_real_recorder_and_sets_the_timeline_origin(self):
        """End to end through the real AudioRecorder: the epoch becomes the
        recording's origin, so the timeline starts with the call rather than
        with whichever leg was captured first."""

        rec = AudioRecorder()
        t = _make_transport(recorder=rec)
        self._drive_start(t)
        assert rec._stream_epoch is not None
        assert rec._t0 == rec._stream_epoch
        assert not rec._stream_epoch_missing

        # And the epoch-vs-anchor alignment is measured on the first frame,
        # which is the number needed before the epoch could anchor placement.
        rec.caller_timestamp({"timestamp": "0"}, rec._stream_epoch + 0.4)
        assert rec._stream_epoch_divergence == pytest.approx(-0.4)

    def test_no_recorder_is_not_an_error(self):
        t = _make_transport(recorder=None)
        self._drive_start(t)          # must not raise
        assert t._stream_sid == "MZ_test"
