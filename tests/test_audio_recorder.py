"""Unit tests for AudioRecorder.

Covers WAV format correctness, channel separation, timeline placement,
edge cases (empty/no-op), and the save+upload persistence path.

No external dependencies — uses stdlib audioop/wave/array only.

Run:
    cd voice-gateway
    python -m pytest tests/test_audio_recorder.py -v
"""
from __future__ import annotations

import array
import asyncio
import audioop
import io
import struct
import wave
from unittest.mock import patch

from vogon.gateway.audio.audio_recorder import AudioRecorder, _build_timeline

# ── Helpers ───────────────────────────────────────────────────────────────────

def _mulaw_silence(n: int) -> bytes:
    """n bytes of mulaw silence (0xFF encodes linear 0)."""
    return bytes([0xFF] * n)


def _mulaw_tone(n: int) -> bytes:
    """n bytes of mulaw non-silence (0x00 decodes to -32124 linear16)."""
    return bytes([0x00] * n)


def _read_stereo(wav: bytes) -> tuple[array.array, array.array]:
    """Parse WAV and return (left_channel, right_channel) as signed short arrays."""
    buf = io.BytesIO(wav)
    with wave.open(buf) as w:
        raw = w.readframes(w.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return samples[0::2], samples[1::2]


def _open_wav(wav: bytes):
    buf = io.BytesIO(wav)
    return wave.open(buf)


# ── WAV format ────────────────────────────────────────────────────────────────

class TestWavFormat:
    def test_empty_recorder_returns_none(self):
        assert AudioRecorder().to_wav_bytes() is None

    def test_riff_wave_magic_bytes(self):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_silence(160))
        wav = rec.to_wav_bytes()
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"

    def test_stereo_channels(self):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_silence(160))
        with _open_wav(rec.to_wav_bytes()) as w:
            assert w.getnchannels() == 2

    def test_8khz_sample_rate(self):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_silence(160))
        with _open_wav(rec.to_wav_bytes()) as w:
            assert w.getframerate() == 8000

    def test_16bit_sample_width(self):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_silence(160))
        with _open_wav(rec.to_wav_bytes()) as w:
            assert w.getsampwidth() == 2

    def test_frame_count_covers_full_timeline(self):
        # Two 20ms chunks 500ms apart → total span ≥ 520ms → ≥ 4160 frames at 8kHz.
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_silence(160))
        rec.add_caller_chunk(0.5, _mulaw_silence(160))
        with _open_wav(rec.to_wav_bytes()) as w:
            assert w.getnframes() >= 4160


# ── Channel separation ────────────────────────────────────────────────────────

class TestChannelSeparation:
    def test_caller_audio_goes_to_left_channel(self):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_tone(160))
        left, right = _read_stereo(rec.to_wav_bytes())
        assert any(s != 0 for s in left),  "Left channel (caller) should have signal"
        assert all(s == 0 for s in right), "Right channel (agent) should be silent"

    def test_agent_audio_goes_to_right_channel(self):
        rec = AudioRecorder()
        rec.add_agent_chunk(0.0, _mulaw_tone(160))
        left, right = _read_stereo(rec.to_wav_bytes())
        assert all(s == 0 for s in left),  "Left channel (caller) should be silent"
        assert any(s != 0 for s in right), "Right channel (agent) should have signal"

    def test_both_channels_populated_when_both_sides_speak(self):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_tone(160))
        rec.add_agent_chunk(0.0, _mulaw_tone(160))
        left, right = _read_stereo(rec.to_wav_bytes())
        assert any(s != 0 for s in left)
        assert any(s != 0 for s in right)

    def test_caller_and_agent_channels_carry_independent_values(self):
        # Use different mulaw values so the decoded linear16 values differ.
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, bytes([0x7F] * 160))  # mulaw 0x7F
        rec.add_agent_chunk(0.0, bytes([0x3F] * 160))   # mulaw 0x3F — different amplitude
        left, right = _read_stereo(rec.to_wav_bytes())
        # First samples of each channel should differ (different mulaw input).
        caller_val = audioop.ulaw2lin(bytes([0x7F]), 2)
        agent_val  = audioop.ulaw2lin(bytes([0x3F]), 2)
        expected_l = struct.unpack_from("<h", caller_val)[0]
        expected_r = struct.unpack_from("<h", agent_val)[0]
        assert left[0]  == expected_l
        assert right[0] == expected_r
        assert expected_l != expected_r


# ── Timeline placement ────────────────────────────────────────────────────────

class TestTimelinePlacement:
    def test_silence_gap_between_chunks(self):
        """500ms gap between two caller chunks produces silent samples in the middle."""
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_tone(160))   # 20ms at t=0
        rec.add_caller_chunk(0.5, _mulaw_tone(160))   # 20ms at t=500ms
        left, _ = _read_stereo(rec.to_wav_bytes())
        # At t≈250ms (sample ~2000) there should be no signal.
        mid = 2000
        assert all(left[mid - 10 : mid + 10][i] == 0 for i in range(20))

    def test_overlapping_caller_and_agent_at_same_time(self):
        """Both channels carry signal when caller and agent speak simultaneously (barge-in)."""
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_tone(160))
        rec.add_agent_chunk(0.0, _mulaw_tone(160))
        left, right = _read_stereo(rec.to_wav_bytes())
        assert any(s != 0 for s in left)
        assert any(s != 0 for s in right)

    def test_agent_starts_after_caller(self):
        """Agent audio at t=200ms stays silent on right channel at t=0."""
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_tone(160))   # caller only at t=0
        rec.add_agent_chunk(0.2, _mulaw_tone(160))    # agent starts at t=200ms
        _, right = _read_stereo(rec.to_wav_bytes())
        # First 1600 samples (200ms × 8000) on right should be silent.
        assert all(right[i] == 0 for i in range(1600))
        # Right channel at t≥200ms should have signal.
        assert any(right[i] != 0 for i in range(1600, len(right)))

    def test_only_caller_chunks_no_crash(self):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_silence(160))
        assert rec.to_wav_bytes() is not None

    def test_only_agent_chunks_no_crash(self):
        rec = AudioRecorder()
        rec.add_agent_chunk(0.0, _mulaw_silence(160))
        assert rec.to_wav_bytes() is not None

    def test_empty_bytes_chunk_ignored(self):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, b"")
        assert rec.to_wav_bytes() is None  # still empty — the empty chunk was dropped

    def test_multiple_chunks_same_channel_concatenated_on_timeline(self):
        """Three consecutive 20ms chunks produce ~60ms of signal at the start."""
        rec = AudioRecorder()
        t = 0.0
        for _ in range(3):
            rec.add_caller_chunk(t, _mulaw_tone(160))
            t += 0.02
        left, _ = _read_stereo(rec.to_wav_bytes())
        # First 480 samples (60ms × 8kHz) should all be non-zero.
        assert all(left[i] != 0 for i in range(480))


# ── _build_timeline helper ────────────────────────────────────────────────────

class TestBuildTimeline:
    def test_single_chunk_at_t0(self):
        mulaw = _mulaw_tone(8)
        pcm = _build_timeline([(0.0, mulaw)], t0=0.0, total_samples=8)
        assert pcm == audioop.ulaw2lin(mulaw, 2)

    def test_chunk_at_nonzero_offset_has_leading_silence(self):
        mulaw = _mulaw_tone(8)
        # Chunk starts at t=0.5s → sample offset 4000.
        pcm = _build_timeline([(0.5, mulaw)], t0=0.0, total_samples=4008)
        assert pcm[:8000] == bytes(8000), "Leading silence missing"
        assert pcm[8000:8016] == audioop.ulaw2lin(mulaw, 2), "Chunk not at correct offset"

    def test_empty_chunk_list_returns_all_zeros(self):
        pcm = _build_timeline([], t0=0.0, total_samples=160)
        assert pcm == bytes(160 * 2)


# ── Persistence ───────────────────────────────────────────────────────────────

class TestSaveAndUpload:
    def test_no_op_when_no_chunks(self, tmp_path):
        rec = AudioRecorder()
        with patch("vogon.gateway.settings.settings") as mock_cfg:
            mock_cfg.recordings_path = str(tmp_path)
            mock_cfg.s3_enabled = False
            asyncio.run(rec.save_and_upload("call-empty"))
        assert not (tmp_path / "call-empty.wav").exists()

    def test_writes_wav_file_locally(self, tmp_path):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_silence(160))
        with patch("vogon.gateway.settings.settings") as mock_cfg:
            mock_cfg.recordings_path = str(tmp_path)
            mock_cfg.s3_enabled = False
            asyncio.run(rec.save_and_upload("call-001"))
        out = tmp_path / "call-001.wav"
        assert out.exists()
        assert out.stat().st_size > 44   # at least a valid WAV header + data

    def test_written_file_is_valid_wav(self, tmp_path):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_silence(160))
        with patch("vogon.gateway.settings.settings") as mock_cfg:
            mock_cfg.recordings_path = str(tmp_path)
            mock_cfg.s3_enabled = False
            asyncio.run(rec.save_and_upload("call-002"))
        with wave.open(str(tmp_path / "call-002.wav")) as w:
            assert w.getnchannels() == 2
            assert w.getframerate() == 8000

    def test_s3_upload_called_with_correct_args(self, tmp_path):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_silence(160))
        with patch("vogon.gateway.settings.settings") as mock_cfg:
            mock_cfg.recordings_path = str(tmp_path)
            mock_cfg.s3_enabled = True
            mock_cfg.s3_bucket = "test-bucket"
            # The gateway owns S3Store after artifact-store extraction.
            with patch("vogon.gateway.audio.artifact_store.S3Store._s3") as mock_s3:
                asyncio.run(rec.save_and_upload("call-003"))
        mock_upload = mock_s3.return_value.put_object
        mock_upload.assert_called_once()
        args = mock_upload.call_args.kwargs
        bucket, key, data, content_type = (args[k] for k in ('Bucket', 'Key', 'Body', 'ContentType'))
        assert bucket == "test-bucket"
        assert key == "calls/call-003/recording.wav"
        assert data[:4] == b"RIFF"
        assert content_type == "audio/wav"

    def test_s3_upload_skipped_when_disabled(self, tmp_path):
        """The recorder writes to the stores the settings name: with S3 off,
        the only store is the local directory and S3 is never constructed."""
        from vogon.gateway.audio.artifact_store import S3Store, default_recording_stores  # noqa: PLC0415
        with patch("vogon.gateway.settings.settings") as mock_cfg:
            mock_cfg.recordings_path = str(tmp_path)
            mock_cfg.s3_enabled = False
            mock_cfg.s3_bucket = "test-bucket"
            stores = default_recording_stores()
            with patch("vogon.gateway.audio.artifact_store.S3Store.put") as s3_put:
                rec = AudioRecorder()
                rec.add_caller_chunk(0.0, _mulaw_silence(160))
                asyncio.run(rec.save_and_upload("call-004"))
        assert not any(isinstance(store, S3Store) for store, _ in stores)
        s3_put.assert_not_called()
        assert (tmp_path / "call-004.wav").exists()

    def test_s3_failure_does_not_propagate(self, tmp_path):
        """An S3 store that raises is logged and skipped; the local file is
        still written and save_and_upload never raises."""
        with patch("vogon.gateway.settings.settings") as mock_cfg:
            mock_cfg.recordings_path = str(tmp_path)
            mock_cfg.s3_enabled = True
            mock_cfg.s3_bucket = "test-bucket"
            mock_cfg.s3_region = ""
            with patch("vogon.gateway.audio.artifact_store.S3Store.put", side_effect=Exception("S3 down")) as s3_put:
                rec = AudioRecorder()
                rec.add_caller_chunk(0.0, _mulaw_silence(160))
                asyncio.run(rec.save_and_upload("call-005"))  # must not raise
        s3_put.assert_called_once()
        assert (tmp_path / "call-005.wav").exists()

    def test_both_caller_and_agent_audio_present_in_saved_file(self, tmp_path):
        rec = AudioRecorder()
        rec.add_caller_chunk(0.0, _mulaw_tone(160))
        rec.add_agent_chunk(0.0, _mulaw_tone(160))
        with patch("vogon.gateway.settings.settings") as mock_cfg:
            mock_cfg.recordings_path = str(tmp_path)
            mock_cfg.s3_enabled = False
            asyncio.run(rec.save_and_upload("call-006"))
        with wave.open(str(tmp_path / "call-006.wav")) as w:
            raw = w.readframes(w.getnframes())
        samples = array.array("h")
        samples.frombytes(raw)
        left, right = samples[0::2], samples[1::2]
        assert any(s != 0 for s in left)
        assert any(s != 0 for s in right)
