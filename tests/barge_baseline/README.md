# Current barge-in behavior baseline

Characterization suite added 2026-09-08, before the episode/floor
controller integration. These tests call the **real TwilioTransport**;
they do not import `prototypes/barge_sim` or implement the proposed controller.
The original 83 cases freeze the legacy path with `barge_controller_enabled=False`.
`test_target_behavior.py` now runs the 11 former expected failures as ordinary
regressions against the integrated controller. `test_floor_integration.py`
covers the new lifecycle and publishing behavior. See [INTEGRATION.md](INTEGRATION.md)
for the current architecture, deployment switch and event-stream details.
Passing the characterization cases means the observed behavior is unchanged, including known limitations.
It does not mean every behavior is desirable.

From `voice-gateway/`:

```bash
python -m pytest tests/barge_baseline -v
```

For a machine-readable baseline artifact:

```bash
python -m pytest tests/barge_baseline -v --junitxml=/tmp/barge-baseline.xml
```

The fixture uses the real transport constructor and the shared
`tests/transport_fixtures.py`. No credentials, network, provider
charges, native AEC library or VAD installation are required. Settings are
explicitly pinned: open STT, semantic barge enabled, RMS 1200, three frames,
100 ms grace, adaptive threshold and VAD confirmation off, 900 ms decision
window, 700 ms classifier timeout, sentence pacing with 500 ms lead.
Individual cases select alternatives. These are experiment settings, not a
claim about deployed configuration.

## Coverage and observed behavior

| Area | Cases / current result |
|---|---|
| Audio routing | Playback, drain, cooldown and silence × AEC present/absent × open/gated feed. AEC processes active-path audio; silent-path audio bypasses it. Without AEC, active-path audio is suppressed even in open mode. |
| Clean energy | Loud raw input with quiet AEC output does not confirm a candidate. Open STT still receives every cleaned frame. |
| Gate | Short bursts, three consecutive frames, silence breaking the streak, start grace, and sustained audio. Confirmation latches; duration alone adds no persistence override. |
| Optional gate stages | VAD confirms or times out while open STT continues. Noise-floor warmup lowers the adaptive threshold on a quiet line; the same quiet speech fails the fixed threshold. |
| Gated fallback | With confirmation off, the first two onset frames are not forwarded; confirmation and subsequent frames are. |
| Async classifier | Audio continues while a classifier future is pending. Timeout, exception, missing or invalid result absorbs. A result arriving after the window resolves does not cut later. |
| Heuristics | Backchannels absorb; configured floor and safety phrases cut; questions and long speech cut. Unresolved short speech absorbs. `uh-huh` fast-paths, whereas `uh huh` needs the judge. |
| Classifier results | Backchannel absorbs; substantive interruption cuts; answer-to-heard yields at a sentence boundary. Burst pacing converts that answer to a hard cut. |
| Partial revisions | An in-flight classifier result can apply to changed ambiguous text; the current window has no revision token check. |
| Early absorb | Later speech can reconfirm the gate but does not reopen the window while absorption is active. A growing substantive final is emitted as a normal turn without cutting playback on that callback. The suite also pins a final growing into a safety phrase after an early backchannel absorb. |
| Repeated attempts | Separate unresolved STT turns absorb repeatedly without escalating. Only the most recently absorbed text is carried into the next normal final. |
| Protection | Partial and final paths × ordinary protected spans and intro absorb-all × backchannel, floor request, substantive speech, ambiguity and safety. Ordinary protected speech defers requests; intro absorbs them; a directly recognized safety phrase cuts both. |
| Echo text | The same long agent-text fragment cuts on the partial window path and is discarded on the playback-final path. This isolates transcript arbitration, not acoustic echo cancellation quality. |
| STT callbacks | The callbacks installed by `start()` emit actual `UtteranceAbsorbed` / `UtteranceFinal` events. Quiet final-only requests can cut without any energy confirmation. |
| Player | Real `send_response()` and sentence streaming consume fake TTS chunks. Complete, hard-cut and graceful-yield runs check wire audio, provider cancellation, clear messages, report status and pending marks. |
| Delivery | Sentence marks advance heard context; duplicate marks do not. End marks close the ledger. Drain clear emits `DrainCleared` and `PlayoutFinished`; late cleared marks do not credit sentences. Sent and mark-confirmed sentences are distinct. |
| Restart | A new transport `send_response()` clears cancellation and can send before caller EOT. This is scoped to the transport; it does not establish what handler/data-plane guards permit. |
| AEC reference | TTS and ambience send methods feed AEC exactly the bytes placed on the outgoing wire. |

## What the harness controls

The transport, gate, heuristics, final arbitration, sentence player and delivery
logic are real. Input frames are 20 ms of controlled μ-law energy. AEC output
and VAD decisions are injected to isolate routing and policy. STT callbacks
are scripted; classifier futures are controlled; TTS supplies fixed chunks;
Twilio receives captured JSON and returns scripted marks. Window time advances
on an injected clock, while asynchronous tasks still yield to the event loop.
The tests assert outcomes and emitted events, not source-code strings.

This suite does **not** measure native AEC adaptation, recognition accuracy,
classifier accuracy, human speech episodes, real socket buffering/clear
latency, provider cancellation races, or end-to-end Turn API / consent behavior.
In particular, the prototype's cold-start echo recording remains a separate
acoustic regression. Add recorded-audio and actual transport integration
checks for those claims; do not interpret synthetic energy as caller speech.

Existing lower-level regressions remain useful alongside this baseline:

```bash
python -m pytest \
  voice-gateway/tests/barge_baseline \
  tests/test_barge_gate.py \
  tests/test_barge_semantics.py \
  tests/test_barge_drain_window.py -q
```

Validation on 2026-09-08: **83 new cases + 42 existing regressions = 125
passed**. JUnit output from this combined run is at `/tmp/barge-baseline.xml`.
An additional attempt to include `tests/test_barge_floor_request.py`
failed during collection because the installed environment lacks `jellyfish`
(imported through application lifecycle). That existing suite is not included
in the passing count. The new suite exercises configured floor phrases without
importing the application's agent registry.

## Original acceptance gaps (now passing)

Before integration, all 11 cases failed with the specific desired-behavior
assertion under `--runxfail`. Their expected-failure markers have now been
removed; they exercise the live controller and must pass normally. The gap
IDs remain as documentation of why the behavior changed.

| Gap | Cases | Desired behavior |
|---|---:|---|
| GAP-01 | 1 | A correction growing from “okay” reopens the decision and cuts before final/EOT. |
| GAP-02 | 2 | Persistent speech overrides a wrong CONTINUE or unresolved decision, without needing EOT. |
| GAP-03 | 1 | After an unresolved attempt, a distinct second speech episode yields without waiting for another partial. |
| GAP-04 | 1 | A cut verdict for a superseded ambiguous partial cannot cancel playback. |
| GAP-05 | 1 | An agent-only echo transcript does not cause a partial-driven cut before the final echo guard. |
| GAP-06 | 3 | A safety final following an early absorb both cuts and reaches the next turn, across ordinary playback, protected defer, and intro absorb-all. |
| GAP-07 | 2 | A response or silence filler cannot restart playback after a semantic yield while the caller is still talking and no EOT has arrived. |

The persistence/repeat cases use provisional **audio-evidence budgets**, based
on the prototype: 1.4 seconds of positive frames for persistence (1.2 seconds
plus gate allowance), and 300 ms for a second episode (240 ms plus allowance),
separated by 400 ms of quiet. These are not measured wall-clock latency SLOs.
Energy frames stand in for speech-positive evidence; VAD/acoustic validation is
still separate. GAP-05 tests text arbitration with a scripted echo transcript,
not native AEC adaptation. GAP-07 is a transport ownership requirement, not a
claim that every upstream handler currently allows the restart.

```bash
# Legacy baseline plus the integrated acceptance and lifecycle regressions:
python -m pytest tests/barge_baseline -q

# Run just the formerly failing acceptance cases:
python -m pytest \
  voice-gateway/tests/barge_baseline/test_target_behavior.py -q --tb=short
```

Before integration: **125 passed, 11 xfailed** in the combined baseline
and existing regression run. Protection still applies to ordinary repeated
speech; the safety cases use the existing configured breakthrough vocabulary.
Generation rejection after EOT and protected-span completion now have
additional tests in `test_floor_integration.py`.

When changing the pipeline, keep the documented before/after behavior
reviewable. Change a characterization assertion only for an intentional policy
change, explain the new result here, and add the corresponding controller or
integration regression. The legacy characterization assertions remain unchanged.
