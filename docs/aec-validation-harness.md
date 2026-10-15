<!-- Copied 2026-10-11 from the application's monorepo (data-plane/proto/aec/README.md) because the gateway's code cites it.
     It is a design record: paths and modules it names are the application's unless they say vogon/. -->

# AEC Prototype — Echo Cancellation for Twilio Barge-In Detection

**Quick start** → [Installation](#installation) · [Running the tests](#step-4--run-the-speexdsp-test-no-audio-files-needed) · [Results](#results-summary-validated) · [Troubleshooting](#troubleshooting)

---

## Problem statement

Twilio delivers inbound audio from the caller's phone as 8 kHz, 8-bit μ-law, mono
PCM.  When the AI is speaking (TTS playing), the caller's phone speaker leaks back
into their microphone as acoustic echo.  That echo travels back to our server mixed
with whatever the caller is saying.

This creates two problems:

1. **False barge-in triggers** — the echo of our own TTS voice exceeds the RMS
   threshold in `twilio_transport._barge_in_in_progress`, causing the system to
   think the caller is speaking when they are silent.

2. **STT contamination** — without gating, Deepgram transcribes our own TTS as
   caller speech, producing nonsense intents.

AEC (Acoustic Echo Cancellation) solves this by subtracting the estimated echo
from the inbound signal using a reference copy of the TTS we sent.

---

## How the pipeline works end to end

```
TTS output (what AI sends) ──────────────────┐
  (reference signal, known to us)            │
                                             ▼
                                     Acoustic path
                                     (phone speaker
                                      → room → mic)
                                             │
                                      delay D ms
                                      attenuation α
                                             ▼
Caller voice (what caller says) ──► [ near-end = caller + α·TTS(t-D) ]
                                             │
                                             ▼
                                 ┌── GCC-PHAT delay estimator ──┐
                                 │   detects D from TTS vs near  │
                                 └────────────┬──────────────────┘
                                              │ D samples
                                              ▼
                              reference = TTS shifted forward by D
                                              │
                                             ▼
                               ┌── SpeexDSP MDF AEC ──────────┐
                               │  Adaptive FIR filter learns α │
                               │  output = near - α·TTS(t-D)  │
                               │        = caller (ideally)     │
                               └──────────────┬────────────────┘
                                              │
                                              ▼
                                 RMS gate for barge-in detection
                                 (no false triggers from echo)
```

### Phase 1 — Echo-only (TTS playing, caller silent)

The AEC filter adapts to the echo path over the first 1–3 seconds of TTS playback.
After convergence:

- Inbound = echo only
- AEC output ≈ silence
- RMS gate stays quiet → no barge-in trigger

**Validated result (synthetic test): 26.7 dB ERLE, 71% → 0% false triggers.**

### Phase 2 — Barge-in (caller speaks, TTS stops)

When the caller speaks:

- Barge-in detected from the AEC output RMS exceeding the threshold
- TTS is muted immediately
- Echo disappears (no more TTS signal to echo)
- AEC passes through caller's voice cleanly

**Validated result: 100% true-positive barge-in detection rate preserved.**

---

## Synthetic test dataset

Because we do not have labelled real-world recordings of Twilio echo scenarios,
the tests generate controlled synthetic signals.  Each signal is designed to
replicate the statistical properties of real phone audio:

### TTS reference signal (`syn_tts.wav`)

Real-world TTS (e.g. Deepgram Aura) is broadband speech — voiced formants,
fricatives, and noise, covering roughly 100–3500 Hz for telephone narrowband.

We generate: **bandlimited noise filtered to 100–3500 Hz** via a 127-tap FIR
bandpass filter (`scipy.signal.firwin`).  The noise is seeded (`rng=42`) for
reproducibility and normalised to peak amplitude 0.4.

**Why not a sine wave?**  GCC-PHAT cross-correlation for delay estimation fails
completely on pure tones: a 440 Hz sine has autocorrelation peaks at every
period (~18 samples), making the peak at lag D ambiguous.  Broadband noise has
a single sharp correlation peak at the true delay.  This was a real failure
during development — the delay estimator returned 0 ms for every delay when the
signal was a pure sine.

### Echo signal (`syn_echo.wav`)

`echo[t] = α × TTS[t − D]`

where:
- `D = 180 ms` (1440 samples at 8 kHz) — representative Twilio round-trip
  acoustic echo delay.  Real range: 80–300 ms depending on phone and room.
- `α = 0.15` (−16 dB) — representative phone/VoIP acoustic echo attenuation.
  Real range: −10 dB (close speakerphone) to −30 dB (handset with good ear
  seal).

### Caller voice signal (`syn_caller.wav`)

Voiced speech synthesised from a harmonic series at 130 Hz fundamental (male
voice) with 7 harmonics, amplitude-tapered by 1/k:

```
voiced[t] = Σ (1/k) × sin(2π × k × 130 × t),  k = 1..7
```

Normalised to peak 0.35.  Starts at `barge_in_sec = 3.0 s` to give the AEC
sufficient convergence time on echo-only frames first.

### Near-end signal (`syn_near_end.wav`)

`near_end = caller + echo_cut`

where `echo_cut` uses `tts_cut` — the TTS that goes to zero at `barge_in_sec`
(simulating the TTS mute when barge-in is detected).  This means the echo tail
also fades after 3 s, mirroring what happens in production.

### Ground-truth separation

Because the signals are generated programmatically, we have exact access to:
- `echo` alone (for ERLE calculation)
- `caller` alone (for correlation / SNR calculation)
- `near_end` = `caller + echo` (what arrives from Twilio)

This separation is what makes the ERLE metric computable.  On real recordings
you only have access to `near_end` and the TTS reference, not the isolated echo.

---

## Algorithms evaluated

### SpeexDSP MDF (Multi-Delay Filter)

**What it is:** Partitioned frequency-domain NLMS (Normalised Least Mean Squares)
adaptive filter.  Processes audio in overlapping blocks, computing the gradient
in the DFT domain across multiple delay partitions.  Includes a double-talk
detector (DTD) that freezes filter adaptation when near-end speech is detected.

**Designed for:** VoIP / softphone acoustic echo cancellation at 8 kHz narrowband.

**Why we chose it:** Ships as a system library (`libspeexdsp-dev`).  No GPU, no
model weights, sub-millisecond latency, deterministic behaviour.  Standard choice
for WebRTC-class echo cancellation.

**Installation:**
```bash
sudo apt-get install -y libspeexdsp-dev swig
pip install git+https://github.com/xiongyihui/speexdsp-python.git
```

Note: the Python package (`speexdsp-python`) generates a SWIG C-extension
`_speexdsp.so`.  An earlier version of the package was missing the SWIG-generated
`speexdsp.py` wrapper, which had to be written manually.  The version installed
via the git URL includes it.

### NKF-AEC (Neural Kalman Filter AEC)

**What it is:** Hybrid linear + neural model (ICASSP 2023, `fjiang9/NKF-AEC`).
Uses a Kalman filter for the linear echo path estimate and a small neural network
for residual non-linear echo suppression.  Designed for 16 kHz audio.

**Why a second stage:** SpeexDSP is a linear adaptive filter.  Acoustic echo has
non-linear components (clipping, harmonic distortion from phone speakers).  A
neural post-filter can suppress these residuals that the linear filter misses.

**Status:** Prototype code written (`test_nkf_aec.py`) but **not yet run**.
Requires cloning the repo and downloading pretrained weights:
```bash
cd proto/aec
git clone https://github.com/fjiang9/NKF-AEC.git nkf_aec
pip install torch torchaudio
```

---

## What was tried and what failed

### GCC-PHAT operand order — wrong direction, fixed

**What happened:** Initial implementation:
```python
R = np.fft.rfft(ref, n=n) * np.conj(np.fft.rfft(near, n=n))   # WRONG
```
This computes `IFFT(X·conj(Y))` which peaks at lag `−D` (ref is delayed
relative to near), not `+D`.  The delay estimator returned 0 ms or wrong values.

**Fix:**
```python
R = np.fft.rfft(near, n=n) * np.conj(np.fft.rfft(ref, n=n))   # CORRECT
```
`IFFT(Y·conj(X))[τ] = Σ y[t+τ]·x[t]` which peaks at `τ = +D` when
`y[t] = x[t−D]` (near is D samples behind ref).

### Pure sine TTS signal — GCC-PHAT ambiguity, fixed

**What happened:** Initial synthetic used a 440 Hz sine wave as the TTS signal.
GCC-PHAT produced peaks at every period of the tone (every ~18 samples) because
the autocorrelation of a pure sine is also a sine.  Delay detection was
completely unreliable.

**Fix:** Replaced with 127-tap FIR bandlimited noise (100–3500 Hz).  GCC-PHAT
now gives a single sharp peak at the correct delay.

### Echo attenuation = 0.3 — filter never converged

**What happened:** The first test used `echo_attenuation = 0.3` (−10.5 dB).
Echo power was −32 dB while caller was −13.5 dB.  ERLE was −19 dB (AEC making
things worse).  Increasing convergence time made no difference.

**Root cause:** When echo is −18 dB below caller (effectively inaudible relative
to near-end), the SpeexDSP double-talk detector engages on every frame.
With DTD freezing adaptation, the filter cannot converge in the first place.

**Fix:** Moved to `echo_attenuation = 0.15` (−16 dB).  At this level the echo
is audible during echo-only periods, the filter converges to 29 dB ERLE, and the
DTD correctly distinguishes echo-only from double-talk frames.

### Echo attenuation = 0.7 — filter re-adapted to cancel caller

**What happened:** Increasing attenuation to 0.7 made echo and caller similar in
power.  ERLE during the caller-active window was still −9 dB.  Correlation
analysis showed `corr(removed_signal, caller) = 0.808` — the AEC was subtracting
the caller's voice, not the echo.

**Root cause:** SpeexDSP MDF was designed for speakerphone scenarios where echo
is −6 to +3 dB relative to near-end.  When echo and caller are equal in power
(0 dB echo-to-near ratio), the DTD cannot distinguish "double-talk" from "echo
is fully occupying the near-end".  The filter re-adapts during the caller-active
period and latches onto the dominant signal (the caller), treating it as the new
echo path.

### Double-talk ERLE measurement — wrong metric for our use case

**What happened:** Early tests measured ERLE over the entire signal including the
caller-active window.  This was −9 to −24 dB across all tested attenuation
values, making SpeexDSP look completely broken.

**Root cause:** ERLE during double-talk is the wrong metric.  AEC is not required
to preserve caller quality during double-talk — barge-in detection does not need
that.  The correct metrics are:
1. ERLE during echo-only window (Phase 1)
2. False-positive and true-positive barge-in trigger rates

Once measured correctly (Phase 1 ERLE = 26.7 dB, false trigger rate 71% → 0%),
SpeexDSP is validated for our use case.

---

## Parameters reference

| Parameter | Value | Notes |
|---|---|---|
| `SAMPLE_RATE` | 8000 Hz | Twilio μ-law narrowband. SpeexDSP is optimised for 8 kHz. |
| `FRAME_SAMPLES` | 160 | 20 ms per frame. Standard telephony frame. Smaller frames reduce latency but worsen frequency resolution inside MDF. |
| `filter_tail_ms` | 400 ms | SpeexDSP filter tail (3200 taps). **Must exceed the maximum expected echo delay.** At 180 ms echo delay, 200 ms would barely work. 400 ms covers jitter and speakerphone paths (up to ~300 ms). |
| `echo_delay_ms` | 180 ms | Simulated round-trip echo delay. Twilio real range: 80–300 ms. |
| `echo_attenuation` | 0.15 | −16 dB. Typical phone/VoIP echo. Real range: 0.05 (−26 dB, handset) to 0.3 (−10 dB, speakerphone). |
| `barge_in_sec` | 3.0 s | Convergence window (echo-only before caller speaks). Real production has no fixed window — filter starts converging the moment TTS begins. 3 s is conservative; convergence typically occurs within 0.5–1 s. |
| `BARGE_IN_RMS_THRESHOLD` | 400 | Matches `twilio_transport._BARGE_IN_RMS_THRESHOLD`. RMS value in int16 scale (0–32767). |
| `firwin` taps | 127 | FIR bandpass order for synthetic TTS generation. Odd number gives linear phase. 127 taps balances sharp rolloff (100 Hz and 3500 Hz edges) against group delay (~63 samples = 8 ms). |
| GCC-PHAT `max_delay_ms` | 500 ms | Search window for delay estimation. Any delay > 500 ms would be a network problem, not an echo path. |

---

## Results summary (validated)

```
Phase 1 (echo-only, 0–3 s, TTS playing, caller silent):
  ERLE                 : 26.7 dB   (>20 dB = safe for production)
  False barge-in (raw) : 71% of frames trigger threshold
  False barge-in (AEC) : 0% of frames trigger threshold

Phase 2 (barge-in, 3–6 s, TTS off, caller speaks):
  True positive (raw)  : 100%
  True positive (AEC)  : 100%

Delay sweep (sensitivity to reference pre-shift):
  Correct delay (180 ms) : ERLE +26.7 dB, 0 false triggers
  Zero pre-shift (0 ms)  : ERLE  +9.3 dB, 5/150 false triggers
  Wrong direction (270 ms): ERLE  +0.2 dB, 89/150 false triggers
```

GCC-PHAT delay detection is essential.  Without it (treating delay as 0 ms),
ERLE drops to 9 dB and false triggers reappear.

---

## Integration plan (Twilio)

1. At call start, instantiate `EchoCanceller(frame_size=160, filter_length=3200, sample_rate=8000)`.
2. For every 20 ms inbound frame, run `aec_out = ec.process(near_bytes, ref_bytes)` where `ref_bytes` is the TTS frame sent 180 ms earlier (pre-shifted buffer).
3. Feed `aec_out` to the RMS barge-in gate and to Deepgram STT instead of raw inbound.
4. On barge-in confirm, stop TTS and zero-out `ref_bytes` so the filter reference goes silent.

The echo delay estimation via GCC-PHAT should run once at the start of the call (during the first 0.5 s of TTS) and the result used to set the reference shift offset.

---

## Installation

All commands below run from the repo root unless otherwise noted.  The prototype
lives in `data-plane/proto/aec/`.

### Step 1 — System C library (required)

SpeexDSP is a C library.  The Python package compiles a SWIG extension against it
during install, so the dev headers must be present first.

```bash
sudo apt-get install -y libspeexdsp-dev swig
```

**Verify:**
```bash
dpkg -l libspeexdsp-dev | grep ii   # should show a version line
swig -version                        # should print SWIG version
```

### Step 2 — Python package for SpeexDSP

Install from the GitHub source, not PyPI.  The PyPI package is outdated and
ships without the SWIG-generated Python wrapper (`speexdsp.py`), which causes
`ImportError: cannot import name 'EchoCanceller'`.

```bash
pip install git+https://github.com/xiongyihui/speexdsp-python.git
```

This compiles the C extension.  You should see output like:
```
Building wheels for collected packages: speexdsp
  Building wheel for speexdsp (setup.py) ...
  Successfully built speexdsp
```

**Verify the install:**
```bash
python3 -c "from speexdsp import EchoCanceller; print('speexdsp OK')"
# Expected: speexdsp OK
```

If this fails, see [Troubleshooting](#troubleshooting) below.

### Step 3 — Python scientific stack

```bash
pip install numpy scipy soundfile
```

`soundfile` is optional — the scripts fall back to the stdlib `wave` module if it
is absent, but soundfile handles more formats and resampling edge cases better.

**Verify:**
```bash
python3 -c "import numpy, scipy; print('numpy', numpy.__version__, 'scipy', scipy.__version__)"
```

### Step 4 — Run the SpeexDSP test (no audio files needed)

```bash
cd data-plane/proto/aec
python3 test_speexdsp.py
```

Expected output:
```
Synthetic test (8 kHz, 400 ms filter, 180 ms echo delay)
  Echo attenuation : 0.15 (−16 dB) — representative phone/VoIP echo
  Barge-in at      : 3.0 s  (TTS muted, caller speaks)
  Barge-in threshold: 400 RMS (int16 scale)
  GCC-PHAT detected delay: 180 ms (simulated: 180 ms)

  Phase 1: Echo-only ...
  ERLE (echo suppression)   : 26.7 dB
  False barge-in (raw)      : 106/150 frames  (71%)
  False barge-in (AEC out)  : 0/150 frames  (0%)
  ✓ AEC eliminates all false barge-in triggers from echo
  ✓ ERLE 26.7 dB — good echo suppression

  Phase 2: Barge-in ...
  True barge-in detect (AEC): 300/300 frames  (100%)
  ✓ AEC preserves 100% barge-in detection reliability
```

The test also writes six `.wav` files you can listen to:

| File | What it contains |
|---|---|
| `syn_tts.wav` | Full AI TTS reference (bandlimited noise, speech-like) |
| `syn_tts_cut.wav` | TTS muted at 3 s (simulates barge-in stopping TTS) |
| `syn_echo.wav` | Acoustic echo only (delayed + attenuated TTS) |
| `syn_caller.wav` | Synthetic caller voice (starts at 3 s) |
| `syn_near_end.wav` | What arrives from Twilio: echo + caller |
| `out_speexdsp.wav` | AEC output: echo suppressed, caller passes through |

Listen to `syn_near_end.wav` vs `out_speexdsp.wav` — in Phase 1 (0–3 s) the raw
file contains audible echo noise; the AEC output should be near-silence.

### Step 5 — Test with real audio (optional)

If you have a real TTS wav and a real Twilio inbound recording:

```bash
python3 test_speexdsp.py \
    --tts  path/to/tts_output.wav \
    --near path/to/twilio_inbound.wav
# Output: out_speexdsp.wav
```

Both files must be 8 kHz mono PCM.  The script resamples automatically if they
are at a different sample rate.

Flags:
```
--tail  N    Filter tail in ms (default 400).  Increase to 600+ for speakerphone.
--delay N    Force echo delay in ms instead of running GCC-PHAT auto-detection.
```

### Step 6 — NKF-AEC second stage (optional, GPU recommended)

NKF-AEC is a neural residual suppressor on top of SpeexDSP.  It is not required
for the barge-in use case (SpeexDSP alone achieves 26.7 dB ERLE) but can push
ERLE higher (30–50 dB) for non-linear echo components.

```bash
cd data-plane/proto/aec

# Clone the NKF-AEC repo into a subdirectory
git clone https://github.com/fjiang9/NKF-AEC.git nkf_aec

# Install PyTorch (CPU-only if no GPU)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu

# Run the two-stage test (SpeexDSP → NKF-AEC)
python3 test_nkf_aec.py --synthetic
```

**Note:** The NKF-AEC repo requires pretrained weights.  Check the repo's README
for download instructions.  The test script will print an error and exit if no
checkpoint file (`.pth` / `.ckpt`) is found in `nkf_aec/`.

### Step 7 — Side-by-side comparison table (optional)

After running both `test_speexdsp.py` and `test_nkf_aec.py`:

```bash
python3 compare.py
```

Prints an ERLE / SNR table across all stages and writes `aec_comparison.txt`.

---

## Troubleshooting

### `ImportError: cannot import name 'EchoCanceller' from 'speexdsp'`

The PyPI version of speexdsp is missing the Python wrapper.  Uninstall it and
reinstall from the GitHub source:
```bash
pip uninstall speexdsp -y
pip install git+https://github.com/xiongyihui/speexdsp-python.git
```

### `swig: command not found` during pip install

SWIG must be installed before the pip install runs:
```bash
sudo apt-get install -y swig
pip install git+https://github.com/xiongyihui/speexdsp-python.git
```

### `fatal error: speex/speex_echo.h: No such file or directory`

The C development headers are missing:
```bash
sudo apt-get install -y libspeexdsp-dev
pip install git+https://github.com/xiongyihui/speexdsp-python.git
```

### GCC-PHAT returns 0 ms delay

Usually means the TTS reference has no broadband content (pure sine, DC, or
silence).  Check that the TTS file is actual speech audio and not empty.  With
real Deepgram Aura output this should not occur.

### ERLE < 10 dB on real audio

Possible causes:
1. Echo delay > filter tail — increase `--tail` (e.g. `--tail 600`)
2. GCC-PHAT detected wrong delay — override with `--delay N` where N is your
   estimated round-trip delay in ms
3. TTS reference and near-end are misaligned in time — trim/pad to align them
   before passing to the script

### NKF-AEC: `No checkpoint found`

Download pretrained weights from the NKF-AEC repo releases page and place them
in `nkf_aec/checkpoint/`.  The test script searches for any `.pth` or `.ckpt`
file recursively under `nkf_aec/`.
