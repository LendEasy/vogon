# Call ambience — liveliness through background sound

**Status:** Phases 1–3 + control-plane config implemented. Off by default.
See [Configuration reference](#configuration-reference) for every knob.
**Surface:** transport layer only (`app/transport/`). No agent, LLM, pipeline, or eval involvement.
**Owner concern flagged:** this is a collections agent with AI-disclosure duties — see [Compliance boundary](#compliance-boundary).

## Why

The single biggest "this is a robot" tell on these calls is **dead air** between
turns. While the LLM + tools run (STT-final → first TTS chunk), the line goes
digitally silent. A human agent's line never does — it carries room tone, and
when you ask a question the agent audibly *looks it up* (typing, a paused
"mm-hmm"). Silence reads as "the call dropped"; ambient continuity reads as
"someone is there, working on it."

Two facts make this worth doing carefully rather than casually:

1. **The widget (browser WebSocket, raw PCM) has true digital silence between
   turns.** Twilio/PSTN calls already carry network comfort noise, so room tone
   matters *most* on the widget. Typing-in-the-gap matters on both.
2. **This is latency theatre with a real basis.** The typing cue lands exactly
   in the STT-final → first-TTS-chunk window — the moment a human agent *would*
   be typing. It converts our worst perceived-latency moment into "looking that
   up," without lying about what the system is.

## The three sound types (proposals, expanded)

### 1. Continuous room tone — the foundation

A seamless 30–60s office-ambience loop at ~**−30 dBFS** (barely above the noise
floor) playing for the whole call. The line is never fully silent, so a
pipeline stall no longer reads as a dropped call. It also masks the transport's
pre-first-chunk TTS retry (`chunks_sent == 0` silent-retry) — the gap becomes
genuinely inaudible instead of a suspicious hole.

- **Duck ~6 dB whenever TTS is speaking** so it never competes with the voice.
  The duck ramps in/out over ~60 ms (attack/release) to avoid a click.
- On the widget it plays continuously (fills true silence). On Twilio it is
  optional and lower-priority (PSTN already carries comfort noise); the win
  there is the typing cue, not the room tone.

### 2. Typing as a latency mask — the main lever

A short keyboard burst that lives **only** in the STT-final → first-TTS-chunk
gap. Timing rules (all deterministic, driven by events the pipeline already
emits):

- **Start ~250 ms after STT-final** (`_last_utterance_t`). Typing the instant
  the caller stops sounds robotic — nobody does that.
- **Stop with a fast (~100 ms) fade the moment the first TTS chunk is ready.**
  Never type over our own voice.
- **Stop instantly on barge-in** (`_tts_cancel` set by STT-interim or
  `client.barge_in`). An agent who keeps typing while you interrupt reads as
  ignoring you.
- **3–5 burst recordings, chosen without immediate repeat** so successive turns
  don't play the identical clip. (Selection is index-rotated, not RNG —
  `Math.random`/wall-clock are unavailable in the deterministic core; the async
  layer supplies a per-turn counter.)

If first-chunk latency is already low (< ~400 ms) the typing never fires — there
was no gap to fill. The cue is proportional to the actual wait.

### 3. Sparse one-shots — optional polish

A distant phone ring or chair creak at low probability, **never during speech
and never overlapping another one-shot**. Cheap once the mixer exists; easy to
overdo. Ship 1 + 2 first; add these only if calls still feel sterile. Kept in
Phase 3 behind config, off by default.

## Architecture

### The one hard DSP rule: mix in linear PCM, never μ-law

μ-law is a *nonlinear* companding of amplitude — you cannot add two μ-law bytes
and get the sum of their signals. All mixing happens in **linear 16-bit PCM**.
The widget path is already linear16 (24 kHz), so it mixes directly. The Twilio
path carries μ-law 8 kHz, so a frame is `ulaw2lin → mix → lin2ulaw` at the
transport boundary (160 samples/frame — cheap). This is why the mixer core is
format-agnostic linear16 and the μ-law conversion lives at the edge.

### Delivery is bursty; playback is real-time

Outbound frames are **burst-sent** ahead of real-time — the TTS loop dumps
20 ms frames as fast as Deepgram produces them, and the client (Twilio jitter
buffer / browser `AudioContext`) plays them back on a 20 ms clock. Consequence:

- **During TTS**, mixing ambience into each outbound frame is real-time-correct
  — the listener hears exactly one ambience frame per 20 ms because that is the
  playback cadence. The ambience cursor advances one frame per outbound frame.
- **Between turns / in the latency gap**, no TTS frames exist, so continuous
  ambience needs a **separate paced task** that emits ambience-only frames on a
  20 ms wall-clock timer. This "playout clock" is the one genuinely new async
  component (Phase 2).

### Components

```
┌─────────────────────────────────────────────────────────────┐
│ AmbienceMixer  (Phase 1 — pure, no I/O, fully unit-tested)   │
│   • decoded PCM loops + typing bursts + one-shots (cursors)  │
│   • seamless loop wrap                                        │
│   • duck gain smoother (attack/release ramp) + hard clamp    │
│   • typing state machine: idle→armed→typing→stopping         │
│   • next_frame(n_samples, tts_frame=None) -> linear16 bytes  │
│   driven by explicit calls: on_tts_start/stop, arm_typing,   │
│   fire_typing, stop_typing, tick(dt) — deterministic         │
└─────────────────────────────────────────────────────────────┘
        ▲ pure calls                         ▲ pure calls
┌───────┴────────────────┐        ┌──────────┴──────────────────┐
│ AmbiencePlayout (P2)   │        │ VoiceTransport / Twilio (P2/3)│
│ 20 ms paced task:      │        │ • mixes ambience into TTS     │
│  emits ambience frames │        │   frames while speaking       │
│  between turns;        │        │ • arms typing on STT-final    │
│  steps aside during TTS│        │ • kills it on 1st chunk/barge │
└────────────────────────┘        └───────────────────────────────┘
```

The mixer is **pure**: no asyncio, no sockets, no clock. It advances only when
called. Every timing decision (when to arm typing, when to duck) lives in the
transport/playout layer, wired to signals that already exist:
`_last_utterance_t` (STT-final), first-chunk detection, `_tts_cancel`
(barge-in). This keeps the DSP unit-testable and the integration thin.

### Assets

Source WAVs live in `app/transport/ambience/assets/`, decoded **once at load**
to linear16 mono and pre-resampled to both native rates (**8 kHz** Twilio,
**24 kHz** widget) so the hot path never resamples. Until real recordings are
dropped in, the loader falls back to a **deterministic synthetic generator**
(seeded, no RNG/wall-clock) — filtered noise for room tone, click trains for
typing — so the feature is testable and runnable without committing binary
audio. Real assets supersede the synthetic source by presence.

## Phases

| Phase | Scope | Risk | Verifiable by |
|---|---|---|---|
| **1** | Pure `AmbienceMixer` + loader + synthetic sources + config (disabled) | none (no wiring) | unit tests (frame math) |
| **2** | **Widget: client-side ambience** (see the pivot below) | low (no server hot-path) | listening + Deepgram WER spot-check |
| **3** | **Twilio μ-law boundary (server-side `AmbienceMixer`) + paced task + one-shots** — implemented | medium (gated off) | listening + phone test |

### Phase 2 pivot — the widget mixes itself; the server-side mixer is for Twilio

The Phase 1 plan assumed Phase 2 would stream server-mixed PCM into
`VoiceTransport`. Reading the widget client changed that decision, and the
evidence is worth recording:

- **The widget only plays audio inside a `tts_stream_start`→`tts_stream_end`
  envelope**, scheduling each binary frame gaplessly on the Web Audio clock and
  running it through a SPEAKING/ACTIVE state machine + barge-in monitor.
  Injecting server frames *between* turns would fight that machine, and
  streaming continuous room tone frame-by-frame is ~384 kbps of hiss over the
  socket, forever, per call.
- **The client already receives both edges of the think-gap.**
  `server.thinking` fires the instant the caller's transcript is finalised (gap
  start); `server.tts_stream_start` / `server.tts_interrupted` mark the end. So
  the widget can time typing and ducking off messages it already gets — **no new
  server messages, no server audio, no hot-path change.**

Therefore Phase 2 on the widget is **client-side**: the browser renders a room
loop through a `GainNode` (ducked on `tts_stream_start`, unducked on
`_end`/`_interrupted`) and fires a typing burst armed on `server.thinking`,
cancelled if TTS starts before the delay. The only server change is a
config block on `session_ready` so the client knows whether ambience is on.

This means the **server-side `AmbienceMixer` (Phase 1) is not used by the
widget** — it is the tool for the *dumb* Twilio client (Phase 3), where the
server controls every byte on the wire and there is no smart client to duck or
loop locally. Two clients, two correct strategies; `sources.py`'s synthesis
approach is mirrored (not shared) in the widget's JS renderer. This is the
honest split, not a compromise.

Files (Phase 2): `frontend/booking-widget.js` (ambience subsystem +
`thinking`/`tts_stream_start`/`_end`/`_interrupted` hooks),
`vogon/websocket/handler.py` (`session_ready.ambience` block). Gated by
`settings.ambience_enabled` (default False).

**Phase 1 (this commit).** `app/transport/ambience/{mixer.py, loader.py,
sources.py}` + `tests/test_ambience_mixer.py`. Settings added, `ambience_enabled`
default **False**. Nothing calls the mixer yet — zero runtime impact.

**Phase 2 (implemented, client-side — see the pivot above).** Widget renders a
room loop through a `GainNode` and a typing burst, driven by the control
messages it already receives (`server.thinking` arms typing;
`tts_stream_start` ducks + stops typing; `_end`/`_interrupted` unduck).
`session_ready` carries the `ambience` config. Gated by
`settings.ambience_enabled` (default False). Acceptance: sounds right on the
widget **and** a Deepgram WER spot-check with ambience on shows no
transcription regression — the room bed leaks from speaker to mic, but
`getUserMedia({echoCancellation:true})` (already set) should cancel it; the bed
is also quiet (~−24 dB via `AMB_BASE_ROOM_GAIN`) and ducked under speech.
Verify, don't assume. Not reachable by the eval suite (no audio in replay).

**Phase 3 (implemented — server-side, Twilio).** This is where the Phase-1
`AmbienceMixer` finally runs: the phone client is dumb, so the server mixes.

- **μ-law boundary** (`_do_tts_stream`): each 160-byte speech frame is
  `ulaw2lin → mixer.next_frame(tts_frame=…) → lin2ulaw`, folding the ducked
  room bed into the voice so it doesn't vanish when the agent talks. Mixing is
  linear16; μ-law is only the wire format.
- **Gap-only paced task** (`_ambience_loop`): a real-time 20 ms emitter that
  fires **only during the think-gap** (`_gap_active`: STT-final → TTS start),
  never in the idle period after a response. Typing is immediate
  (`ambience_typing_delay_ms` default 0) and quiet (`ambience_typing_gain`
  default 0.15, well below the bed/speech).

  **Why gap-only (the latency fix, learned from a live Twilio call):** the TTS
  path burst-sends frames seconds ahead of real time and leans on Twilio's
  playout buffer. An earlier version emitted ambience continuously the instant
  TTS was *sent* (`_tts_playing` False, but the audio still buffered/unplayed) —
  that refilled Twilio's buffer at exactly its drain rate, pinning it at the
  burst-lead depth. The caller then heard everything delayed by that depth, and
  it ratcheted up across turns (compounding lag). Restricting emission to the
  think-gap fixes it: the gap begins only after the caller has listened + spoken
  for seconds, by which point the buffer has drained, so gap frames play
  concurrently with the agent's compute and add ~no latency; between a response
  and the next turn the server stays silent and the buffer drains. Room tone
  still plays *under speech* (mixed into TTS frames — no extra frames, no lead)
  and *during the gap*. One-shots are effectively dormant on Twilio in gap-only
  mode (they needed idle emission) — acceptable, they are off by default.
- **AEC**: between-turn frames go through `_send_ambience_frame`, which feeds the
  AEC reference (so the bed is cancelled from inbound STT, not mistaken for
  caller speech) but deliberately does **not** advance the TTS recorder cursor
  (that per-utterance timeline is owned by `_send_media_frame`).
- **Silence detection is safe**: `seconds_since_last_speech` keys off the Twilio
  mark echo, not outbound frame flow, so continuous ambience does not disturb
  follow-up prompting. Verified against `test_silence_detection.py`.
- **One-shots**: a third mixer layer (`trigger_oneshot`), off by default,
  gated in the loop to fire only between turns, never overlapping, no sooner
  than `ambience_oneshot_min_gap_sec`. Synthetic distant-ring / chair-creak
  until real `oneshot_*.wav` assets land.
- **Config**: resolved per call from `assistant_cfg["ambience"]` falling back
  to `settings.ambience_*`. Off by default. The control-plane wires this
  first-class: a nullable `ambience` JSONB column on `assistant_config`, a typed
  `AmbienceSettings` schema (validated bounds), and an authoritative export line
  (after the `extras` spread). Both consumers honor it — the Twilio transport
  reads the snake_case dict directly; the widget's `session_ready` block maps it
  to the camelCase keys the browser reads. NULL means "unset" → data-plane
  settings defaults. SQLite dev DBs migrate on startup
  (`app.database._run_migrations`); prod Postgres via `migrate_add_ambience.py`.

Verify on a real phone call (not reachable by CI or the eval suite): room tone
under speech, typing in the gap, no jitter underrun, and a Deepgram WER check
with the bed on (AEC-cancelled + quiet, so low risk).

## Configuration reference

Every knob, its default, and which surface it affects. Two ways to set them:
**globally** via the data-plane's `settings` (env vars — uppercase the name,
e.g. `AMBIENCE_ENABLED=true`), or **per-persona** via the control-plane
`assistant_config.ambience` block (`AmbienceSettings`, snake_case, overrides the
global default for that workflow). NULL/unset per-persona → the global default.

| Key (per-persona / settings) | Default | Range | Surface | What it does |
|---|---|---|---|---|
| `enabled` / `ambience_enabled` | **false** | bool | both | Master switch. Off = the whole feature is inert. |
| `room` / `ambience_room_tone` | true | bool | both | The room-tone bed (during the gap + under speech). |
| `typing` / `ambience_typing` | true | bool | both | The keyboard cue in the think-gap. |
| `oneshots` / `ambience_oneshots` | false | bool | phone only | Sparse ring/creak texture. Dormant in gap-only mode (see Phase 3). |
| `typing_delay_ms` / `ambience_typing_delay_ms` | **0** | 0–5000 | both | Hold after the borrower stops before typing starts. 0 = immediate (the "we're on it" cue). |
| `typing_gain` / `ambience_typing_gain` | **0.15** | 0.0–1.0 | both | Typing loudness. 1.0 was ~8× the bed and far too loud; 0.15 sits at/just above the bed. Lower to ~0.08 to tuck it clearly under. |
| `room_gain` / `ambience_room_gain` | 1.0 | 0.0–4.0 | **phone only** | Trim on the (already-quiet) room source. The widget uses a fixed bed level instead (`AMB_BASE_ROOM_GAIN` = 0.11) and ignores this. |
| `duck_factor` / `ambience_duck_factor` | 0.5 | 0.0–1.0 | both | How far the bed drops while the agent speaks — see [Ducking](#ducking). |
| `oneshot_min_gap_sec` / `ambience_oneshot_min_gap_sec` | 25.0 | 1–600 | phone only | Minimum quiet seconds between one-shots. |

Client-only constants in `booking-widget.js` (not server-configurable): the bed
level `AMB_BASE_ROOM_GAIN` (0.11), and the click-free ramp time-constants
`AMB_DUCK_TC` (0.04 s) / `AMB_TYPING_FADE_TC` (0.03 s). Their server equivalents
are `AmbienceConfig.duck_ramp_ms` (60 ms) and `typing_fade_ms` (100 ms).

Why the split by surface: the **widget** is a smart Web Audio client that
renders and loops the bed itself, so it only needs the toggles, the duck factor,
and the typing timing/volume — its bed level is a local constant. The **phone**
(Twilio) has the server put every byte on the wire, so it honors the full set
including `room_gain` and the one-shot knobs.

### Ducking

"Ducking" is the standard audio technique of automatically **lowering one sound
while a more important one plays**, so the important one stays clear — the term
comes from broadcast, where music "ducks" under the announcer. Here the
room-tone bed ducks under the agent's TTS: while the agent is speaking, the bed
is attenuated so it never competes with intelligibility; when speech stops, the
bed returns to full level.

`duck_factor` is the multiplier applied to the bed's gain **while speaking**:

```
bed level while speaking = room_gain × duck_factor      (widget: AMB_BASE_ROOM_GAIN × duckFactor)
bed level otherwise       = room_gain                    (full)
```

- `0.5` (default) → the bed drops to half amplitude, about −6 dB, under speech.
- `1.0` → **no ducking**; the bed stays at full level under the voice.
- `0.0` → the bed is fully silenced whenever the agent speaks.

Lower = more aggressive ducking (quieter bed under speech). The transition is
**not instant** — it ramps over ~60 ms on the server (`duck_ramp_ms`) / a 0.04 s
time-constant on the widget (`AMB_DUCK_TC`), applied both on the way down
(attack, when speech starts) and back up (release, when speech ends), so the
level change is click-free.

Where it fires: **widget** — the room `GainNode` ramps to
`AMB_BASE_ROOM_GAIN × duckFactor` on `tts_stream_start` and back on
`tts_stream_end` / `tts_interrupted`. **Phone** — `mixer.set_speaking(True)` on
send_response / `False` in its finally; the ducked bed is mixed into the TTS
frames. (In gap-only mode the bed only exists under speech and during the gap;
`duck_factor` governs the under-speech portion.)

## Compliance boundary

This is a **collections** agent under AI-disclosure obligations (we already
treat AI-call-consent revocation as a distinct legal posture — see
`dnc_handler`). Typing and neutral room tone are defensible: the system
genuinely *is* looking data up, and a quiet office floor is not a
representation about who is calling. **Voices / chatter / anything implying a
human colleague is out of bounds** — it cuts against the disclosure story and
could read to a regulator as a deliberate deception pattern. Disclosure wording
is unchanged by this feature. Profiles ship room-tone + typing only.

## Testing

The eval suite (text replay) cannot reach any of this — there is no audio in a
replay. Verification is:

- **Phase 1:** `tests/test_ambience_mixer.py` — pure frame math: loop-wrap
  continuity (no discontinuity at the seam), duck ramp monotonicity, int16
  clamp on sum overflow, typing lifecycle (arm→fire→stop, stop-on-barge),
  silence-in → ambience-out, deterministic synthetic source reproducibility.
- **Phases 2–3:** recorded listening checks + a Deepgram WER spot-check with
  ambience on (widget mic leakage). These are manual/rig tests, not CI.
