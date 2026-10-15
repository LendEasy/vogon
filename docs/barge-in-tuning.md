# Barge-in detection — diagnosis & tuning

**Surface:** transport layer (`app/transport/`). Phone path (`twilio_transport.py`)
is the focus — it is where barge-in was over-aggressive on loud backgrounds.
**Status:** Tier 1 implemented; Phase 0 measured (2026-07, four live calls);
**Phase A (adaptive threshold) + Phase B1 (BargeGate FSM + webrtcvad confirm)
implemented, flag-gated, default off.** Deepgram backend (B2): measured no-go.
**Not reachable by the eval suite** (no audio; the eval harness never runs the
transport barge path) — validation is a live phone/rig test.

> **Companion:** [`voice-timing-constants.md`](voice-timing-constants.md) covers
> the **hardcoded** constants of the same subsystem — the delivery, silence and
> TTS-socket bounds that are *not* env-tunable and should not be changed without
> evidence. This doc is the knobs; that one is the invariants.

## Settings reference (all flags, env-var form)

Everything is settings-tunable and defaults to the safest behaviour; the two
Tier-2 layers are **off** until explicitly enabled. Set as env vars on the
data-plane (uppercase) and restart.

| Env var | Default | Phase | Meaning |
|---|---|---|---|
| `BARGE_IN_RMS_THRESHOLD` | 1200 | Tier 1 | fixed int16-RMS gate on cleaned frames; also the adaptive cold-start fallback |
| `BARGE_IN_CONSECUTIVE_FRAMES` | 3 | Tier 1 | debounce: consecutive over-threshold frames (×20 ms) before the pre-gate fires |
| `BARGE_IN_GRACE_MS` | 100 | Tier 1 | ignore barge this long after TTS starts |
| `BARGE_IN_FRAME_LOG` | false | Phase 0 | per-frame RMS CSV + post-AEC WAV for measurement / the timeline RMS lane |
| `BARGE_IN_ADAPTIVE_ENABLED` | **false** | A | threshold tracks the line's own noise floor instead of the fixed value |
| `BARGE_IN_ADAPTIVE_ALPHA` | 0.05 | A | noise-floor EMA weight (noise frames only) |
| `BARGE_IN_ADAPTIVE_MARGIN` | 7.0 | A | `threshold = clamp(margin × floor, min, max)` |
| `BARGE_IN_ADAPTIVE_MIN` | 600 | A | sensitivity ceiling — quiet lines gate at this (catches soft speech) |
| `BARGE_IN_ADAPTIVE_MAX` | 2600 | A | strictness ceiling — stays below direct caller speech |
| `BARGE_IN_ADAPTIVE_WARMUP_FRAMES` | 50 | A | noise frames (≈1 s) before the estimate replaces the fixed threshold |
| `BARGE_IN_CONFIRM` | **off** | B1 | `off` = energy debounce confirms directly (Tier 1); `local_vad` = webrtcvad confirm window |
| `BARGE_IN_VAD_AGGRESSIVENESS` | 3 | B1 | webrtcvad mode (0 permissive … 3 strict; 3 is the only measured-usable mode) |
| `BARGE_IN_CONFIRM_FRAMES_M` / `_N` | 3 / 5 | B1 | confirm = M speech-positive of the last N provisional frames |
| `BARGE_IN_CONFIRM_TIMEOUT_MS` | 400 | B1 | provisional window before ABORT (nothing audible happened) |
| `BARGE_IN_CONFIRM_ENERGY_FLOOR_FRAMES` | 3 | B1 | K sub-threshold frames inside the window → early ABORT |
| `BARGE_PROTECT_INTRO` | **true** | protected | first utterance (recorded-line disclosure) plays to completion — barge defers instead of cutting; safety phrases still cut |
| `BARGE_PROTECT_FAREWELL` | **true** | protected | call-ending utterances (farewell, DNC ack) get the same protection |

Recommended test-session enablement: `BARGE_IN_ADAPTIVE_ENABLED=true
BARGE_IN_CONFIRM=local_vad BARGE_IN_FRAME_LOG=true`. Instant rollback:
`BARGE_IN_CONFIRM=off` + `BARGE_IN_ADAPTIVE_ENABLED=false` restores Tier-1
exactly. If barging feels sluggish, lower `BARGE_IN_CONFIRM_FRAMES_M` to 2
before touching anything else.

## How barge-in works (three mechanisms)

| Surface | Trigger | Debounce | Grace | Basis |
|---|---|---|---|---|
| **Twilio (phone)** `twilio_transport.py` | `clean_rms > threshold` during TTS (post-AEC) | Tier 1: **N consecutive frames** | Tier 1: **grace after TTS start** | energy (RMS) after AEC |
| **Widget backend** `voice_transport.py` | Deepgram STT interim during TTS, or `client.barge_in` | (client) | (client) | speech (STT) + client VAD |
| **Widget client** `booking-widget.js` | mic `rms > 0.2` | 4 consecutive frames | 400 ms | energy (mic VAD) |

The phone path cannot use STT-interim barge: inbound audio is echo-gated while
TTS plays, so it never reaches Deepgram to produce interims. Hence the RMS gate.

### The gate no longer gates the STT feed (2026-07, open feed)

Historically this gate did two jobs: deciding barges AND acting as the valve
on what caller audio reached Deepgram during agent playback (unconfirmed
frames were dropped as presumed echo). The second job clipped quiet and
overlapping caller speech once pacing made playback occupy real time — see
[`stt-open-feed.md`](stt-open-feed.md). With `stt_feed_mode=open` (default)
the AEC-cleaned audio streams to Deepgram at all times and this gate keeps
only its real job: deciding whether to cut playback. `stt_feed_mode=gated`
restores the old valve.

### The semantic layer above this gate (2026-07, in progress)

This document covers the ENERGY gate: detecting *that* the caller is speaking.
[`full-duplex-semantic-barge-in.md`](full-duplex-semantic-barge-in.md) adds the
layer that decides *what that speech means for the floor*: a confirmed barge no
longer cuts immediately — a yield-pending window + a Cerebras verdict classify
it as backchannel (absorb silently, `BARGE_ABSORBED`, audio keeps playing),
answer-to-heard (yield + bind), substantive interrupt (cut, as today), or
safety (cut immediately). The FSM below is unchanged; its CONFIRMED output
becomes a *candidate* rather than a verdict.

### The barge window: send + playback-drain + cooldown (2026-07 fix)

The send loop runs **ahead of real time** (no pacing — "buffer debt"), so
`_tts_playing` goes False when the stream finishes *sending*, seconds before
Twilio finishes *playing* the buffered remainder. The detection window is
therefore three phases, all gated in `handle_text_frame`:

| Phase | Signal | Barge detection |
|---|---|---|
| Send window | `_tts_playing` | active — cancel breaks the stream, then `clear` |
| **Playback-drain window** | `_playback_draining` (a `tts_end_*` mark sent, echo not yet received; age-capped `_DRAIN_WINDOW_MAX_SEC`) | active — confirmed barge → `_clear_buffered_playback()` (Twilio `clear`, drain state ends, cooldown takes over) |
| Echo cooldown | `now - _tts_ended_at < 0.25s` | active (echo suppression tail) |

Pre-fix, the drain term was missing: detection went dark the moment sending
ended, so a caller barge mid-reply left the buffered audio playing to the end
(the drain-clear branch was unreachable). The mark echo is the exact playback-
complete signal; pending marks are the exact drain signal. AEC stays aligned
throughout — its reference FIFO is fed at send rate and drained at inbound
rate, so it time-aligns to playback by construction — with two conditions
(2026-10-09, `voice-gateway/tests/aec/`): a Twilio `clear` must discard the
FIFO too (`_send_clear` → `AECProcessor.discard_reference`), or the next
reply's reference lags by the discarded audio; and the GCC-PHAT delay
estimate opens its 600 ms window on reference SPEECH and is only locked when
its peak is sharp, because a window of silent reference (the inbound backlog
before the opening's first TTS byte) returned 0 ms against a 200 ms echo and
was locked for the whole call.

## Why loud background falsely triggered it (pre-Tier-1)

1. **Single-frame trigger** — one 20 ms frame over threshold called
   `_tts_cancel.set()`. A slam / cough / bump / one loud TV syllable cut the
   agent off. No sustained-energy requirement (the widget needs ~64 ms).
2. **Threshold in the loud-background band** — `_BARGE_IN_RMS_THRESHOLD = 500`
   (int16 RMS) ≈ −33 dBFS. Comfort noise ~−45/−50 dBFS (below), speech ~−20/−15
   dBFS (above), but **loud ambient — TV, café, car, a second person — sits
   ~−30/−25 dBFS, above 500.**
3. **Energy only, no speech discrimination** — pure `audioop.rms()`; can't tell
   speech from a fan or music.
4. **AEC does not remove it** — the SpeexDSP AEC cancels the *far-end reference*
   (our TTS/ambience echo), NOT independent *near-end* background noise. Loud
   room noise passes through AEC intact. (The AEC's "0% false barge" figure was
   a synthetic, echo-only validation — ambient background was never in it.)
5. **No start grace** — nothing protected the response onset / a caller
   backchannel ("mm-hmm") at the very start of the agent's turn.
6. **Fixed threshold** — 500 regardless of the actual line's noise floor.

## Tier 1 (implemented)

All in `twilio_transport.py`, gated by new `settings`:

| Setting | Default | Meaning |
|---|---|---|
| `barge_in_rms_threshold` | 1200 | int16 RMS a cleaned frame must exceed to count toward a barge (~−26 dBFS — above loud background, below speech). Raised from 500. |
| `barge_in_consecutive_frames` | 3 | consecutive over-threshold frames required before cancelling TTS (3 × 20 ms = 60 ms of sustained energy). Resets on any sub-threshold frame. |
| `barge_in_grace_ms` | 100 | ignore barge for this long after TTS starts, so the response onset / a brief backchannel doesn't cut it off. Kept low for responsiveness. |

- **Debounce:** a per-frame counter increments while `clean_rms > threshold`,
  resets to 0 on any frame below it; TTS is cancelled only when the counter
  reaches `barge_in_consecutive_frames` (and the grace has elapsed). Cleaned
  audio still forwards to STT from the first over-threshold frame so the
  utterance onset is not lost — only the *cancel* is debounced.
- **Event log:** every confirmed barge logs one structured INFO line —
  `raw_rms`, `clean_rms`, `frames`, whether TTS was in flight vs. buffer-drain —
  plus a `barge_in_event` metric, so the threshold/debounce can be tuned from
  real call data. Transients suppressed by the debounce log at DEBUG.
- **Defaults are provisional** — chosen from the dBFS analysis above, not yet
  from measured call data. The event log exists precisely to ground them.

## Tier 2 — design (Phases A + B1 since implemented — see the plan section below)

Tier 1 rejects *transient* noise but is still an energy test, so it does not
catch **sustained loud non-speech** (TV, music, road, fan) or handle **soft
speech in a loud room**. Tier 2 attacks that from two angles. Both are
**gated on measurement** (see [Measurement gate](#measurement-gate-before-any-tier-2-code));
documenting them here is not a decision to build them.

### Option A — Adaptive threshold (data flow, not a state machine)

Replace the fixed `barge_in_rms_threshold` with one that tracks the caller's
own ambient floor, so the bar is always *relative to their background*:

```
per cleaned frame (post-AEC), while NOT in a confirmed barge:
    if clean_rms <= threshold:            # a "noise" frame
        floor ← α·clean_rms + (1−α)·floor # EMA — running estimate
    threshold ← max(floor_min, floor × margin)
```

- Uses a **running EMA**, not the existing `_noise_rms_sum` cumulative average
  (that is a whole-call mean, too slow and history-dominated to gate on).
- **Only noise frames update the floor** — never speech/barge frames — or the
  floor inflates and desensitizes barge (missed real barges). Slow α + a hard
  `floor_cap` bound this.
- **Cold start:** fall back to the fixed Tier-1 threshold until enough noise
  frames accumulate.
- **Ambience-aware:** computed on post-AEC `clean_rms`, so it already reflects
  the *residual* after our echo/room-tone reference is cancelled.
- **Fixes:** sustained loud background. **Does not fix:** soft speech below the
  adaptive bar, or background that is itself speech-level. Low risk, no latency,
  self-contained — the recommended first Tier-2 step.

### Option B — Speech-gated two-stage barge (state machine)

Split the decision: the Tier-1 energy+debounce becomes a **pre-gate** that opens
a *provisional* listening window; a pluggable **`confirm(window) → bool`**
predicate decides whether it was actually speech before TTS is cancelled.

```
                    ┌───────────────────────────────────────────────┐
                    │                     IDLE                        │
                    │  TTS playing normally. Energy pre-gate counts   │
                    │  the clean_rms>threshold streak (Tier-1         │
                    │  debounce). No confirm backend engaged.         │
                    └────────────────────────┬────────────────────────┘
                       streak ≥ N  AND  start-grace elapsed
                       (Tier-1 trigger — but does NOT cancel yet)
                                             │
                                             ▼
                    ┌───────────────────────────────────────────────┐
                    │             PROVISIONAL_LISTENING               │
                    │  TTS KEEPS PLAYING  ← proposed (see fork below) │
                    │  Feed cleaned frames to confirm(window).        │
                    │  confirm_timeout_ms timer armed.                │
                    └───────┬───────────────────────────────┬─────────┘
              confirm()==true│                              │ timeout elapsed, OR
              (it was speech)│                              │ clean_rms<threshold for K frames
                             ▼                              ▼
        ┌────────────────────────────────┐   ┌──────────────────────────────────┐
        │        CONFIRMED_BARGE          │   │            ABORT → IDLE           │
        │  cancel TTS (_tts_cancel.set    │   │  never cancelled TTS; reset the   │
        │  in-flight, else _send_clear);  │   │  streak; RELEASE any confirm-     │
        │  forward audio to STT as the    │   │  backend turn state opened during │
        │  real turn; latch               │   │  the window (see echo risk #3).   │
        │  _barge_in_in_progress.         │   └──────────────────────────────────┘
        │  → resets to IDLE at TTS end.   │
        └────────────────────────────────┘
```

**The load-bearing fork — what TTS does during `PROVISIONAL_LISTENING`.** This
choice *is* B's cost/benefit; there is no clean answer:

- **Keep playing until confirm (proposed).** On a *real* barge the agent talks
  over the caller for the whole confirm window; on a *false* trigger nothing is
  audible (TTS never hiccups). Makes **confirm latency the barge-quality
  metric** — which is the strongest argument for the local-VAD backend below.
- **Duck/pause TTS during provisional (rejected).** On every false trigger (a
  background spike) the agent's own speech audibly hiccups — more noticeable
  than the status quo.

Because we keep playing, and because on Twilio `_send_clear` isn't instant (TTS
is burst-sent seconds ahead into the jitter buffer — same physics as the
ambience-lag work), the talk-over window = confirm latency **+** flush latency.
So confirm must be *tight*.

**Pluggable confirm predicate — one machine, two backends.** The state machine
is discriminator-agnostic; `confirm(window)` has two implementations:

| Backend | Latency | Accuracy | Touches ASR conn? | Risk |
|---|---|---|---|---|
| **Local VAD** (WebRTC / Silero / Speex) | ~1 frame | good | **no** | may under-discriminate speech-like noise |
| **Deepgram interim** (true "B") | ~100–300 ms | best | **yes** | STT turn-state pollution (below) |

Build the machine once; try the **local-VAD backend first** (tight latency, no
ASR contact); swap in Deepgram only if the local VAD under-discriminates.

**What `confirm()` is doing — a VAD primer.** Voice Activity Detection decides,
per short frame (~10–30 ms), *is this speech or not-speech (silence/noise/
music)?* Detectors form a ladder, crude → robust: **(1) energy threshold** —
"louder than X?" (exactly our Tier-1 gate; fooled by any loud sound); **(2)
energy + features** — zero-crossing rate, spectral tilt, harmonicity; **(3)
statistical noise-model** — track the noise spectrum, compute a per-band
speech-presence probability from SNR (WebRTC VAD, Speex VAD); **(4) DNN** — a
small net trained on speech vs. noise (Silero, RNNoise). What separates them is
the hard case — *non-stationary, speech-like noise* (TV dialogue, café babble,
music with vocals): energy and statistical VADs fail there, DNN VADs mostly win.
A local VAD is rung 3–4; Deepgram's endpointer is DNN-grade rung 4. Both are a
big step above our rung-1 energy gate; that step is the whole point of Tier 2.

**Which local VAD — and a dependency caveat.** "SpeexDSP VAD" is *not* free
here: we depend on `speexdsp-python` but use only its **EchoCanceller**
(`speex_echo`, the MDF echo canceller). Speex's VAD lives in a *separate*
module, the preprocessor (`speex_preprocess`), which this binding does **not**
appear to expose — so using it would need a binding change. Speex's VAD also has
a weak reputation (statistical, fooled by speech-like noise; long de-emphasised
in favour of just reading the probability). So for the local-VAD slot prefer
**WebRTC VAD** (`webrtcvad` — mature, 8 kHz-native, tiny, purpose-built for
exactly this) or a small DNN (**Silero**) over Speex. The pluggable `confirm()`
is precisely so the specific detector is swappable — pick from measured
discrimination on the Layer-2 fixtures, don't hard-wire one.

**Echo risk is turn-state pollution, not just a phantom barge.** The Deepgram
backend forwards provisional audio into the *same persistent connection* that
serves the real turn. Residual echo/noise there can (a) produce a phantom
interim → phantom barge, and worse (b) drive a StartOfTurn→EndOfTurn cycle that
emits a spurious **final** → enqueues an `UtteranceEvent` → **the agent answers
its own echo**. A provisional listen that doesn't confirm cannot be un-fed, so
the `ABORT` transition must explicitly discard/guard any Deepgram turn opened in
the window (tag provisional-window finals and drop them). The local-VAD backend
sidesteps this entirely — a decisive point in its favour.

**Instrument every transition** (for tuning the fork + thresholds from real
calls): `provisional_open` (triggering `clean_rms`, streak), `confirm` (with
**confirm-latency** — the distribution tells us whether the talk-over window is
tolerable), `abort_timeout`, and `suspected_echo_confirm` (a confirm whose
interim text overlaps the agent's current TTS).

### Measurement gate (before any Tier 2 code)

Both options want real-call numbers first — the Tier-1 `barge_in_event` /
debounce-suppressed logs already emit them. Capture on a quiet line **and** the
loud-background scenario, and — because ambience now mixes room tone into the
outbound TTS *and* the AEC reference — capture **ambience ON and OFF**
separately (the post-AEC residual that a confirm step would see includes
room-tone residual; B may be viable bed-off but not bed-on).

- **A** needs the `clean_rms` noise-floor distribution to set `α`, `margin`,
  `floor_min`, `floor_cap`.
- **B** is explicitly gated on: (a) measured residual-echo RMS during TTS
  showing the confirm won't phantom-fire, **and** (b) A + local-VAD proving
  insufficient. Absent both, B stays documented, not built.

### Recommended sequence

1. **A (adaptive threshold)** — low risk, no latency; grounded in the captured
   floor distribution.
2. **Local-VAD confirm** (the state machine above with a local-VAD backend —
   WebRTC/Silero preferred over Speex; see the dependency caveat) — speech
   discrimination without Deepgram's latency/pollution.
3. **Deepgram two-stage (B)** — only if 1+2 are insufficient and the echo
   measurement clears it.

### Deferred (Tier 3 / later)

- **Per-persona tuning** — expose the barge knobs through control-plane
  `assistant_config` like the ambience block. Global settings only for now.
- **Widget/phone parity** — reconcile the two energy VADs once the phone path
  is tuned.

## Tier 2 — implementation & test plan

Sequenced per the recommendation above. Every phase is **flag-gated and
default-off**, so it is inert in production until switched on for a live test —
the same posture as the ambience work.

### Phase 0 — Measurement (gates everything else) — IMPLEMENTED

Echo and noise-floor are physical; they cannot be synthesised faithfully, so
this phase runs on real calls.

- **Instrument (built):** `barge_in_frame_log` (settings, default off). When on,
  `TwilioTransport` collects one row per inbound frame — `t_rel_s, raw_rms,
  clean_rms, tts_playing, gap_active, streak, barge_in_progress` — plus the
  **post-AEC μ-law stream** (during the echo-gated window; raw ≡ clean outside
  it), and at call end writes `<recordings_path>/<call_id>.frames.csv` +
  `<call_id>.clean.wav`. The WAV is *exactly* the audio a VAD confirm step
  would see, replayable offline. Flag off → one `is not None` check per frame,
  nothing collected. Contract pinned by `tests/test_barge_frame_log.py`.
- **Analyse (built):** `proto/barge/analyze_frames.py` — clean-RMS percentile
  distributions split by tts_playing (noise floor vs during-TTS residual), a
  Tier-1 gate replay (would-be barge counts across threshold × debounce
  candidates, tunable without new calls), and `--vad-sweep` (webrtcvad modes
  0–3 over the clean WAV: speech-positive rate during vs outside TTS — the
  local-VAD discrimination table). `--selfcheck` generates synthetic data and
  runs the whole pipeline. `webrtcvad` is pinned in requirements.

**Runbook (the part that needs a human on a phone):**

1. On the test box set `BARGE_IN_FRAME_LOG=true` (leave ambience however the
   scenario needs) and restart the data-plane.
2. Make the call matrix — each a real Twilio call, ~2 min:
   | # | scenario | ambience |
   |---|---|---|
   | 1 | quiet room, no interruptions | off |
   | 2 | quiet room, 2–3 deliberate barges mid-agent-speech | off |
   | 3 | loud background (TV/music near the handset), NO barges | off |
   | 4 | loud background + 2–3 deliberate barges | off |
   | 5–8 | repeat 1–4 | on |
3. Collect `storage/recordings/<call_id>.frames.csv|.clean.wav` and run
   `python3 proto/barge/analyze_frames.py <call>.frames.csv --vad-sweep`
   (multiple CSVs at once is fine).
4. Read off: **(a)** Option A params — floor from the quiet-call distributions,
   margin from the gate replay row that yields 0 false barges on call 3 while
   still catching the scripted barges on call 4; **(b)** the webrtcvad mode
   whose during-TTS positive rate is ~0 on calls 1/3 (no barges) while barges
   in calls 2/4 still read positive; **(c)** the Deepgram go/no-go from the
   during-TTS residual on calls 5–8 (ambience-on).

**Gate:** the floor distribution sets Option A's `α`, `margin`, `floor_min`,
`floor_cap`; the VAD sweep picks the local-VAD mode + M-of-N; the during-TTS
residual-echo distribution is the **B go/no-go**.

### Local-VAD confirm — tuning parameters (now live defaults; see the Settings reference)

The pre-gate keeps the Tier-1 knobs (`barge_in_rms_threshold`,
`_consecutive_frames`, `_grace_ms`). The confirm layer adds:

| Setting (implemented) | Role | How Phase 0 set it |
|---|---|---|
| `barge_in_confirm` = `off` \| `local_vad` | backend selector; `off` = Tier-1 behaviour, instant rollback | — |
| `barge_in_vad_aggressiveness` (0–3) | webrtcvad mode: 0 = permissive (more noise passes), 3 = strict (may clip soft speech) | the sweep: highest mode with ~0 during-TTS positives on no-barge calls that still fires on scripted barges |
| `barge_in_confirm_frames_m` / `_n` | confirm = M speech-positive of the last N frames in the provisional window (smooths single-frame VAD flicker) | start 3-of-5; widen to 4-of-8 if sweep shows flicker on real speech |
| `barge_in_confirm_timeout_ms` | provisional window before ABORT | ≥ N×20 ms + margin; start 400 |
| `barge_in_confirm_energy_floor_frames` | K consecutive sub-threshold frames inside the window → early ABORT | keep small (2–3); the transient already ended |

Talk-over budget (keep-playing fork) ≈ pre-gate (60 ms) + M×20 ms + flush —
with 3-of-5 that is ~160 ms of decision latency, inside the ~300 ms Layer-4 bar.

### Phase A — Adaptive threshold — IMPLEMENTED (flag-gated, default off)

Lives inside `BargeGate` (`app/transport/barge_gate.py`): an EMA noise floor
over **noise frames only** (never speech/barge frames, never inside a
provisional window), `threshold = clamp(margin × floor, min, max)`, fixed
threshold until `adaptive_warmup_frames` noise frames accumulate.

**Params set from the 2026-07 Phase 0 measurement** (quiet-line floor p50 6–35,
noisy 85–300; caller speech 1500–4300; soft-speech misses in the 600–1100 band):
`alpha=0.05, margin=7, min=600, max=2600, warmup=50`. Quiet line → threshold
drops to **600** (fixes the measured soft-speech misses at fixed 1200); loud
background → rises to **2100–2600** (rejects the p99≈1000 residual spikes).
Enable with `BARGE_IN_ADAPTIVE_ENABLED=true`.

### Phase B1 — State machine + local-VAD confirm — IMPLEMENTED (flag-gated, default off)

- **Pure `BargeGate`** (`app/transport/barge_gate.py`): per-frame inputs
  `(now, clean_rms, tts_playing, frame)`; returns a `FrameVerdict`
  (cancel/forward/noise + audio-log events) — the transport performs the I/O
  it prescribes. Full transition table pinned by `voice-gateway/tests/test_barge_gate.py`
  (16 tests, no audio/VAD lib — the confirm is an injected lambda).
- **Confirm backend:** `BARGE_IN_CONFIRM=local_vad` → **webrtcvad mode 3**
  (measured: the only usable mode — 2.6–3% false during TTS on quiet lines;
  modes 0–2 fire on echo residual 17–97%). M-of-N = **3-of-5**, timeout 400 ms,
  energy-floor abort 3 frames. webrtcvad unavailable → degrades to Tier-1
  semantics, never a dead gate. Never touches the ASR connection.
- **Provisional semantics:** TTS keeps playing during the window; provisional
  frames are **buffered and flushed to STT on confirm** (onset not lost) and
  dropped on abort (no STT leakage from unconfirmed windows).
- **Instrumentation:** `barge_provisional_open`, `barge_confirmed`
  (+`confirm_latency_ms`, VAD M/N), `barge_abort` (timeout | energy_floor) —
  all rendered in the audio-timeline barge lane (purple markers).

**Measured limitation (expected):** TV/music background is speech-like — on the
loud-background Phase 0 call webrtcvad mode 3 was 65% speech-positive during
TTS, so the VAD cannot reject it; that is the adaptive threshold's job
(the two layers are complementary, enable both).

**Enable for a test session:**

```bash
BARGE_IN_ADAPTIVE_ENABLED=true
BARGE_IN_CONFIRM=local_vad
# optional tuning: BARGE_IN_VAD_AGGRESSIVENESS=3  BARGE_IN_CONFIRM_FRAMES_M=3
#                  BARGE_IN_CONFIRM_FRAMES_N=5    BARGE_IN_CONFIRM_TIMEOUT_MS=400
```

### Phase B2 — Deepgram confirm backend  *(only if gated in — measured NO-GO for now)*

Phase 0 data: during-TTS residual spikes to p99 1000–3700 and webrtcvad reads
65–94% "speech" during TTS on noisy calls — forwarding to Deepgram would
phantom-confirm routinely on exactly the calls that need help. Stays shelved
unless quiet-line-only enablement becomes worth it.

- Second `confirm` backend `barge_in_confirm=deepgram`. Adds the provisional
  forward into the persistent STT connection **plus** the ABORT turn-state
  guard (tag provisional-window finals, drop them so the agent can't answer its
  own echo).
- Built only if Phase 0 clears the echo gate **and** A + local-VAD proved
  insufficient.

## Test plan

The eval suite has no audio and never runs the transport barge path, so it is
irrelevant here. Coverage is four layers, most-deterministic first:

### Layer 1 — Pure unit tests (CI, no audio)

- **Adaptive floor:** feed `clean_rms` sequences to `_update_noise_floor` —
  floor rises on sustained noise, is unaffected by in-barge/speech frames,
  respects `floor_cap`, cold-starts to the fixed threshold. Deterministic.
- **`BargeGate` FSM (highest value):** drive with synthetic per-frame inputs and
  a **stub `confirm`**; assert the full transition table — IDLE→PROVISIONAL on
  debounce+grace, PROVISIONAL→CONFIRMED when the stub returns true,
  PROVISIONAL→ABORT on timeout and on energy-drop, ABORT resets the streak and
  fires `on_release`, `on_cancel` fires exactly once. Pins all control flow with
  zero audio.
- **Confirm backends in isolation:** local-VAD wrapper logic with a stubbed VAD;
  Deepgram-backend ABORT guard given a fake STT that emits a spurious final →
  assert no `UtteranceEvent` is enqueued (the self-echo guard).

### Layer 2 — Recorded-fixture integration (CI-able; the discrimination test)

- A small fixture set of ~5–10s **8 kHz mono** clips — clean speech barge
  ("stop, wait a second"), TV/news chatter, music, café babble, road noise,
  comfort-noise/silence — fed frame-by-frame through `handle_text_frame` (AEC
  passthrough/bypass, real `BargeGate`).
- **Assert discrimination:** speech clip → barge; TV/music/babble/road → **no**
  barge; silence → no barge. This is what makes "tells speech from loud noise"
  testable offline — the thing energy alone can't do. (Synthesise the
  deterministic ones — noise, tones — in-test; source a few short real clips for
  chatter/music, self-recorded or freely licensed.)
- **Self-echo guard:** feed the agent's *own* TTS audio as the provisional-window
  input (simulating AEC leakage) with the Deepgram backend + fake STT → assert
  ABORT discards the turn.

### Layer 3 — Measurement replay (semi-automated, real-call data)

Phase 0's CSVs + analysis script, re-run against each tuning candidate to
confirm the chosen `α`/`margin`/threshold separate the measured noise-floor and
speech distributions with margin. Grounds params in data, not guesses.

### Layer 4 — Live rig / manual acceptance (real phone; the only full-fidelity test)

Scenario × ambience(on/off) matrix, with pass bars:

| Scenario | Expected | Metric / bar |
|---|---|---|
| Quiet line, genuine interruption | barges promptly | barge latency (confirm-latency log) < ~300 ms |
| Loud TV/music, **no** caller speech | **no** barge | false-barge rate ≈ 0 over K agent turns |
| Loud background **+** genuine interruption | barges | missed-barge rate = 0 |
| Soft-spoken caller interrupts | barges | missed-barge rate = 0 |
| Backchannel "mm-hmm" during grace | no barge | false-barge rate ≈ 0 |
| (B only) any barge | agent never answers its own echo | self-echo incidents = 0 |

### Coverage & gaps (be explicit)

| What | Layer | Testable offline? |
|---|---|---|
| Adaptive-floor math, FSM transitions, guards | 1 | ✅ fully |
| Speech-vs-noise **near-end** discrimination | 2 | ✅ with recorded fixtures |
| Self-echo turn-state guard logic | 1–2 | ✅ with a fake STT |
| Real **duplex echo** residual (AEC on a live acoustic loop) | 4 only | ❌ needs a phone |
| End-to-end barge latency incl. Twilio jitter-buffer flush | 4 only | ❌ needs a phone |

The two ❌ rows are exactly why Phase 0 measurement gates the build and Layer 4
is the final acceptance — Layers 1–2 prove the logic, but only a live call
proves the echo behaviour and true latency.
