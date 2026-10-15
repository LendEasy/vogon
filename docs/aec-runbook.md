# Acoustic Echo Cancellation — Setup Runbook

This document captures everything done to get echo cancellation working end-to-end:
the problem, the prototype, the production implementation, and the specific issues hit
during deployment on the remote host.

**TL;DR for re-deploying on a new server:** jump to
[Remote Host Setup](#remote-host-setup).

---

## Problem

On speakerphone and landline calls, the agent's TTS audio plays through the caller's
speaker and is picked up by the caller's microphone. That echo travels back to our
server over Twilio's media stream, mixed with whatever the caller is actually saying.

Two concrete failure modes:

1. **False barge-in** — the echo of our own TTS voice exceeds the RMS threshold in
   `twilio_transport._barge_in_in_progress`, so the system thinks the caller is
   speaking when they are silent. TTS is cut off and the agent proceeds on no speech.

2. **STT contamination / hallucinated utterances** — without gating, Deepgram
   transcribes our own TTS output as caller speech. In the realtime pipeline, OpenAI's
   built-in VAD fires on the echo and the model hallucinates a plausible "caller"
   response — often a non-sequitur continuation of what the agent just said.

Callers on modern smartphones with hardware AEC are unaffected. The problem surfaces on
speakerphone, desk phones, older handsets, and in call centre environments.

---

## Prototype phase

**Directory:** `data-plane/proto/aec/` in the application's monorepo; its README is copied here as `aec-validation-harness.md`

Before touching production code, we built a standalone validation harness that
generates synthetic phone audio and measures echo suppression quality.

### What we tested

`test_speexdsp.py` generates:
- `syn_tts.wav` — bandlimited noise (100–3500 Hz) simulating Deepgram Aura TTS
- `syn_echo.wav` — TTS delayed 180 ms + attenuated by −16 dB (representative
  Twilio phone echo)
- `syn_caller.wav` — voiced speech starting at 3 s (barge-in moment)
- `syn_near_end.wav` — echo + caller (what arrives from Twilio)
- `out_speexdsp.wav` — AEC output

We used SpeexDSP MDF (adaptive filter designed for 8 kHz telephony).

### Issues hit during prototyping

**GCC-PHAT delay estimator had reversed operand order.**
Initial implementation was `rfft(ref) * conj(rfft(near))`, which peaks at lag −D (the
wrong direction). Changed to `rfft(near) * conj(rfft(ref))` — peaks at +D. Without
this fix the delay estimator returned 0 ms or wrong values for every input.

**GCC-PHAT was ambiguous on pure-sine TTS.**
The first synthetic TTS signal was a 440 Hz sine wave. GCC-PHAT cross-correlation
produces a peak at every period of the tone (~18 samples), not just at the true delay.
Fixed by replacing the sine with 127-tap FIR bandlimited noise — one sharp correlation
peak at the correct delay.

**Echo attenuation of 0.3 (−10 dB) caused SpeexDSP to fail entirely.**
At that level, SpeexDSP's double-talk detector engaged on every frame because the
echo power was within 18 dB of the caller power. With DTD freezing adaptation, the
filter never converged. Moved to 0.15 (−16 dB), which matches real-world phone echo
more accurately. Filter converged and achieved 26.7 dB ERLE.

**ERLE measured over the wrong window.**
Early tests computed ERLE over the whole signal including the caller-active window, which
gave −9 dB (AEC making things worse). AEC is not expected to preserve quality during
double-talk — it only needs to suppress echo when the caller is silent. Switched to
measuring ERLE during the echo-only phase (0–3 s). Result: 26.7 dB.

### Validated results

```
Phase 1 (echo-only, TTS playing, caller silent):
  ERLE                 : 26.7 dB
  False barge-in (raw) : 71% of frames trigger threshold
  False barge-in (AEC) : 0% of frames

Phase 2 (barge-in, TTS off, caller speaks):
  True positive (raw)  : 100%
  True positive (AEC)  : 100%
```

26.7 dB ERLE with 0% false triggers: validated safe for production.

Full details and parameter rationale in
[`aec-validation-harness.md`](aec-validation-harness.md).

---

## Production implementation

### AECProcessor (`data-plane/app/transport/aec.py`)

Wraps SpeexDSP MDF with two additions:

**Reference FIFO pacing.** Twilio plays agent audio at real-time rate (8000 bytes/s)
but our TTS stream delivers chunks to Twilio 5–10× faster than real-time. If we fed
those burst-rate chunks directly to SpeexDSP, the reference signal would advance much
faster than the near-end signal and the filter would see a meaningless alignment.

Fix: `feed_reference()` pushes chunks to a FIFO deque. `process()` pops exactly one
160-sample frame per call — paced to the inbound clock. Both sides now advance at
real-time rate.

**GCC-PHAT delay estimation.** The acoustic path from Twilio's speaker to the caller's
microphone introduces an unknown delay (80–300 ms depending on phone hardware, network
buffering, Twilio jitter buffer). After 600 ms of audio accumulates, `_run_gcc_phat()`
estimates the delay and sets a pre-shift so SpeexDSP receives the reference aligned in
time with the near-end.

Parameters used in production:

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Frame size | 160 samples (20 ms) | Matches Twilio μ-law packet size |
| Filter tail | 400 ms | Exceeds the longest expected echo delay (~300 ms) |
| Sample rate | 8000 Hz | PSTN / Twilio narrowband |
| GCC-PHAT warmup | 600 ms | Enough audio for a sharp correlation peak |
| Max delay search | 500 ms | Any delay > 500 ms is a network problem, not echo |
| Fallback delay | 180 ms | Used if GCC-PHAT fails (weak/no echo) |

### Integration into both pipelines

**Classic pipeline (`TwilioTransport`):**

- `_send_media_frame` calls `aec.feed_reference(mulaw_bytes)` after sending each frame
  to Twilio.
- `handle_text_frame` calls `aec.process(mulaw_bytes)` on each inbound frame and
  feeds the cleaned audio to Deepgram STT. RMS is measured on both raw and clean audio
  so log entries capture how much echo was present.

**Realtime pipeline (`OpenAIRealtimeTwilioTransport`):**

- `_handle_audio_delta` calls `aec.feed_reference()` after forwarding each delta to
  Twilio.
- `_twilio_reader` calls `aec.process()` on each inbound frame before appending to
  `input_audio_buffer` — OpenAI's VAD sees echo-free audio.

The echo guard (a timing-based fallback that suppressed turns where `speech_started`
fired within 800 ms of estimated Twilio playback end) has been removed. AEC is the
sole echo defence in the realtime pipeline.

### Graceful fallback

`create_aec()` wraps instantiation in a try/except. If `speexdsp` is not installed or
fails to import, it returns `None` and logs a warning. Both transports check for `None`
before calling AEC methods — calls proceed without echo cancellation. No crash, no
silent failure.

---

## Remote host setup

**Target OS:** Amazon Linux 2023 (production server at `/opt/codelexin/`)

### Step 1 — Install system C libraries

SpeexDSP is a C library. The Python binding compiles a native extension (`_speexdsp.so`)
at install time and requires the development headers and SWIG.

```bash
sudo yum install -y gcc-c++ swig speex-devel speexdsp-devel
```

| Package | Purpose |
|---------|---------|
| `gcc-c++` | C++ compiler |
| `swig` | Generates the Python ↔ C++ binding from `speexdsp.i` |
| `speex-devel` | Speex codec headers (indirect dependency) |
| `speexdsp-devel` | SpeexDSP headers — `speex/speex_echo.h` required by the extension |

> **Gotcha:** `speex-devel` and `speexdsp-devel` are separate packages on Amazon Linux.
> Installing only `speex-devel` will succeed but the build will fail with:
> `fatal error: speex/speex_echo.h: No such file or directory`
> Always install both.

Verify headers are present:
```bash
ls /usr/include/speex/speex_echo.h    # must exist
swig -version                          # must print SWIG version
```

### Step 2 — Install the Python package

Install from GitHub source, not PyPI. The PyPI package is outdated and may be missing
the SWIG-generated Python wrapper.

```bash
sudo /opt/codelexin/data-plane/.venv/bin/pip install \
  "speexdsp @ git+https://github.com/xiongyihui/speexdsp-python.git"
```

This compiles the C extension. Expected output:
```
Building wheels for collected packages: speexdsp
  Building wheel for speexdsp (setup.py) ...
  Successfully built speexdsp
```

### Step 3 — Restart the data-plane service

```bash
sudo systemctl restart codelexin-data-plane
```

### Step 4 — Verify AEC is active

```bash
sudo journalctl -u codelexin-data-plane -n 50 | grep "AEC init"
```

Expected (one line per call, at call start):
```
AEC init | filter_tail_ms=400 max_delay_ms=500 warmup_ms=600
```

During a call, ERLE is logged every ~1 second:
```
AEC ERLE | erle_db=31.4 delay_frames=9 ref_fifo=0 total_frames=150
```

Values above 20 dB confirm the filter is converging on real echo. Values below 5 dB
mean no echo is present (caller has hardware AEC on their phone) — which is expected
and fine.

---

## Issue hit during deployment: `speexdsp.py` wrapper missing from the wheel

### What happened

After installing the `speexdsp` package from PyPI (not GitHub), the data-plane service
failed to start with:

```
ModuleNotFoundError: No module named 'speexdsp.speexdsp'
```

or:

```
ImportError: cannot import name 'EchoCanceller' from 'speexdsp'
```

The installed package contained `_speexdsp.so` (the compiled C extension) but not
`speexdsp.py` (the SWIG-generated Python wrapper that re-exports `EchoCanceller`).

### Root cause

The `speexdsp` PyPI package (version 0.1.x) was built before the SWIG wrapper was
stabilised. The wheel ships `_speexdsp.so` but omits `speexdsp.py`, so `from speexdsp
import EchoCanceller` fails.

### Fix applied

Instead of relying on the package to provide `speexdsp.py`, we import the C extension
directly in `AECProcessor.__init__` and define the wrapper inline:

```python
from speexdsp._speexdsp import (
    EchoCanceller_create, EchoCanceller_process, delete_EchoCanceller,
)

class EchoCanceller:
    def __init__(self, frame_size, filter_length, sample_rate):
        self._ptr = EchoCanceller_create(frame_size, filter_length, sample_rate)

    def process(self, near, ref):
        return EchoCanceller_process(self._ptr, near, ref)

    def __del__(self):
        if self._ptr is not None:
            delete_EchoCanceller(self._ptr)
            self._ptr = None
```

This makes `AECProcessor` independent of whether the package includes the wrapper file.
We also switched the install source to `git+https://github.com/xiongyihui/speexdsp-python.git`
which ships a complete wheel, as an additional safeguard.

---

## Troubleshooting

### `swig: command not found` during pip install
```bash
sudo yum install -y swig
```

### `error: command 'g++' failed: No such file or directory`
```bash
sudo yum install -y gcc-c++
```

### `fatal error: speex/speex_echo.h: No such file or directory`
`speex-devel` is installed but `speexdsp-devel` is not:
```bash
sudo yum install -y speexdsp-devel
ls /usr/include/speex/speex_echo.h    # verify
```

### `ModuleNotFoundError: No module named 'speexdsp.speexdsp'`
PyPI package missing wrapper. Reinstall from GitHub source:
```bash
sudo /opt/codelexin/data-plane/.venv/bin/pip uninstall speexdsp -y
sudo /opt/codelexin/data-plane/.venv/bin/pip install \
  "speexdsp @ git+https://github.com/xiongyihui/speexdsp-python.git"
```

### AEC init line absent from logs
AEC silently disabled. Check startup logs for the warning:
```bash
sudo journalctl -u codelexin-data-plane -n 100 | grep -i "aec\|speex"
```
If you see `speexdsp not installed — AEC disabled`, the package is not installed or
failed to import. Follow steps 1–2 above and restart.

### ERLE < 5 dB on every call
Either: no echo present (callers have hardware AEC — this is fine), or the reference
signal is not aligned (ref_fifo is always empty). Check:
```bash
sudo journalctl -u codelexin-data-plane | grep "AEC ERLE"
# If ref_fifo=0 consistently and erle_db is flat-low, check feed_reference() is being called
```

### ERLE 5–15 dB with false barge-ins still occurring
Echo delay may exceed the GCC-PHAT detection window or the filter tail. The estimated
delay is logged:
```
AEC GCC-PHAT | delay_ms=240 delay_frames=12
```
If `delay_ms` is close to `max_delay_ms` (500 ms) or GCC-PHAT is failing (fallback 180
ms used), the filter tail may be too short. Increase `_FILTER_TAIL_MS` in `aec.py` from
400 to 600.

---

## Files

| File | Role |
|------|------|
| `data-plane/app/transport/aec.py` | `AECProcessor`, `create_aec()` factory |
| `data-plane/app/transport/twilio_transport.py` | AEC wired into classic pipeline |
| `data-plane/app/transport/openai_realtime_transport.py` | AEC wired into realtime pipeline |
| `data-plane/requirements.txt` | `speexdsp` dependency (git source) |
| `data-plane/proto/aec/test_speexdsp.py` | Synthetic validation harness |
| `aec-validation-harness.md` (copied from the prototype's README) | Prototype design, algorithm details, all failed attempts |
| `docs/aec-design.md` | Production design reference |
| `docs/aec-installation.md` | Amazon Linux install commands (condensed) |

---

## Other distributions

| OS | Install command |
|----|----------------|
| Ubuntu / Debian | `sudo apt-get install -y gcc g++ swig libspeex-dev libspeexdsp-dev` |
| Amazon Linux 2 / AL2023 | `sudo yum install -y gcc-c++ swig speex-devel speexdsp-devel` |
| Fedora / RHEL | `sudo dnf install -y gcc-c++ swig speex-devel speexdsp-devel` |

Python package install is identical across all distributions:
```bash
pip install "speexdsp @ git+https://github.com/xiongyihui/speexdsp-python.git"
```
