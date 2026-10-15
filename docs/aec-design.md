# Acoustic Echo Cancellation — Design Doc

**Status:** Implemented and deployed  
**Commit:** `2d92e6c`  
**Date:** 2026-06-17

---

## Problem statement

On speakerphone and landline calls, the agent's TTS audio plays through the caller's
speaker and is picked up by the caller's microphone. This echo travels back through
Twilio's media stream to the server.

In the **realtime (S2S) pipeline**, this echo reaches OpenAI's built-in VAD, which
fires `input_audio_buffer.speech_started` on it. OpenAI transcribes the echo (which
contains the agent's own words) and hallucinates a plausible "caller" response —
often a non-sequitur continuation of what the agent just said. The call then proceeds
based on a fabricated caller intent.

In the **classic pipeline**, the echo reaches Deepgram STT, which transcribes it and
feeds it to the planner as a real utterance. Same result: the agent responds to its
own words.

Callers whose phones have hardware AEC (most modern smartphones) are unaffected. The
problem surfaces on speakerphone, desk phones, and older handsets.

---

## Approach

**Acoustic Echo Cancellation (AEC)** subtracts the agent's TTS audio (the "reference"
signal) from the inbound caller audio (the "near-end" signal), recovering the caller's
actual voice. This is the same technique used in conference call hardware and WebRTC.

The implementation uses **SpeexDSP MDF** (Multi-Delay Filter), a production-grade
adaptive filter designed for 8 kHz telephony audio. Unlike a time-domain gate that
simply mutes the microphone during TTS, the adaptive filter removes echo while
allowing real barge-in speech through — the caller can interrupt the agent and be
heard clearly.

---

## Architecture

```
OpenAI Realtime API
        │
        │ audio delta (μ-law, burst)
        ▼
_handle_audio_delta ──── feed_reference() ──► AECProcessor._ref_fifo
        │
        │ forward to Twilio (real-time)
        ▼
    Twilio ────────────── plays audio ──────────► Caller's speaker
                                                         │
                                               acoustic echo + caller voice
                                                         │
                                                  Caller's microphone
                                                         │
                                                   Twilio μ-law frames
                                                         │
                                                         ▼
_twilio_reader ─────── aec.process() ──────────► cleaned μ-law
        │
        │ input_audio_buffer.append
        ▼
  OpenAI Realtime API (VAD sees echo-free audio)
```

The same pattern applies to the classic pipeline:

```
TTS → _send_media_frame → feed_reference()
Twilio inbound → handle_text_frame → aec.process() → Deepgram STT
```

---

## AECProcessor implementation

**File:** `data-plane/app/transport/aec.py`

### SpeexDSP MDF

SpeexDSP MDF is an adaptive filter that learns the acoustic path between speaker and
microphone. Given a reference signal (what the speaker played) and a near-end signal
(what the microphone captured), it estimates and subtracts the echo component.

Parameters used:

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Frame size | 160 samples (20 ms) | Matches Twilio's μ-law packet size |
| Filter tail | 400 ms | Must exceed the longest expected echo delay |
| Sample rate | 8000 Hz | PSTN / Twilio narrowband |

### Reference FIFO pacing

Twilio buffers agent audio server-side and plays it at real-time rate (8000 bytes/s).
Our TTS stream delivers audio to Twilio 5–10× faster than real-time (burst delivery).

If the reference signal were indexed by delivery position, it would advance through
the echo filter 5–10× faster than the near-end signal, making the alignment
meaningless. 

Fix: `feed_reference()` pushes each chunk to a FIFO deque. `process()` pops exactly
one 160-sample frame per call (once per inbound Twilio packet). Both sides advance at
the inbound clock rate. The shift history built inside `process()` therefore maps
frame N to what Twilio was playing at near-end time N × 20 ms.

### GCC-PHAT delay estimation

The acoustic path from Twilio's speaker to the microphone introduces an unknown delay
(phone hardware, network buffering, Twilio jitter buffer). If the filter is given the
reference at the wrong time offset, it cannot converge.

After accumulating 600 ms of audio, `_run_gcc_phat()` estimates the delay:

```
R = rfft(near) × conj(rfft(ref))   # near first: peak at +D, not −D
PHAT = ifft(R / |R|)
delay_samples = argmax(|PHAT|[:max_lag])
delay_frames  = delay_samples // 160
```

The pre-shifted reference fed to SpeexDSP is then `shift_history[-(delay_frames+1)]`.
Before GCC-PHAT completes, the most recent reference frame is used so SpeexDSP has
something to adapt on during warmup.

Fallback: if GCC-PHAT fails (weak echo, silent call), 180 ms is assumed.

### ERLE monitoring

Echo Return Loss Enhancement (ERLE) measures filter effectiveness:

```
ERLE (dB) = 10 × log₁₀(near_power / output_power)
```

Logged every 50 frames (~1 second):

```
AEC ERLE | erle_db=31.4 delay_frames=9 ref_fifo=0 total_frames=150
```

ERLE > 20 dB is considered production-safe. The synthetic validation test achieved
30.9 dB ERLE with a simulated 180 ms echo at −20 dBFS.

---

## Integration points

### Realtime pipeline — `OpenAIRealtimeTwilioTransport`

**Reference feed** (`_handle_audio_delta`, after Twilio send):
```python
if self._aec is not None:
    self._aec.feed_reference(base64.b64decode(delta))
```

**Near-end filter** (`_twilio_reader`, before `input_audio_buffer.append`):
```python
if self._aec is not None:
    send_mulaw = self._aec.process(raw_mulaw)
    send_payload = base64.b64encode(send_mulaw).decode()
else:
    send_payload = payload
```

### Classic pipeline — `TwilioTransport`

**Reference feed** (`_send_media_frame`, after WebSocket send):
```python
if self._aec is not None:
    self._aec.feed_reference(mulaw_bytes)
```

**Near-end filter** (`handle_text_frame`, AEC gate during TTS):
```python
clean_mulaw = self._aec.process(mulaw_bytes)
```

---

## Recording clock skew

`AudioRecorder` places agent audio at `_rec_agent_start_ts + byte_offset/8000`, where
`_rec_agent_start_ts` is set when the first audio delta arrives from OpenAI. Twilio
buffers ~500 ms before playing, so the agent track in recordings appears ~500 ms early
relative to actual playback time.

Consequence: in stereo call recordings, acoustic echo arrives on the caller track after
the agent track ends — not overlapping it. This is expected and does not indicate
missing echo. The recording accurately represents what OpenAI receives; the offset is
only in the perceived timeline when playing back the stereo WAV.

---

## Echo guard (removed)

A timing-based guard (`_ECHO_GUARD_SECS = 0.8`) previously suppressed turns where
`speech_started` fired within 800 ms of estimated Twilio playback end. With AEC
filtering the inbound audio stream before it reaches OpenAI's VAD, this guard is
redundant and was removed. The AEC is the sole echo defence.

---

## Validation

| Test | Result |
|------|--------|
| Synthetic 180 ms echo at −20 dBFS, burst FIFO delivery | 30.9 dB ERLE |
| Real call recording (`recording (1).wav`, caller with hardware AEC) | No measurable echo in recording (expected — hardware AEC on caller's phone suppresses echo before it enters Twilio) |
| Speakerphone validation | **Pending** — requires a test call on a speakerphone or desk phone to produce measurable echo |

To validate on a live call, look for `AEC ERLE | erle_db=...` in logs during a call
made on speakerphone. ERLE > 20 dB confirms the filter is converging on real echo.

---

## Files

| File | Role |
|------|------|
| `data-plane/app/transport/aec.py` | `AECProcessor` class, `create_aec()` factory |
| `data-plane/app/transport/openai_realtime_transport.py` | AEC wired into realtime pipeline |
| `data-plane/app/transport/twilio_transport.py` | AEC wired into classic pipeline |
| `data-plane/requirements.txt` | `speexdsp` dependency (git source) |
| `docs/aec-installation.md` | System dependency install guide |

---

## Dependencies

| Dependency | Version | Source |
|-----------|---------|--------|
| `speexdsp` Python binding | latest | `git+https://github.com/xiongyihui/speexdsp-python.git` |
| SpeexDSP C library | system | `speexdsp-devel` (yum) / `libspeexdsp-dev` (apt) |
| `swig` | system | `swig` (yum/apt) — required to build the binding |
| `numpy` | ≥1.21 | PyPI — used for GCC-PHAT FFT |
