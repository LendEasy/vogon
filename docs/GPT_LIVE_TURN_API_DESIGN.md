<!-- Copied 2026-10-11 from the application's monorepo (data-plane/docs/GPT_LIVE_TURN_API_DESIGN.md) because the gateway's code cites it.
     It is a design record: paths and modules it names are the application's unless they say vogon/. -->

# GPT-Live 1 on the Turn API — strawman design

Status: STRAWMAN for review, 2026-09-11. No code. Replaces the gpt-realtime-2
path (`use_realtime`), which is removed in full (§9).

Every API fact below was observed on this account today, not read from the
docs: openai SDK 3.13.0, four live sessions over G.711 μ-law, both delegation
modes, one real borrower clip. Session logs: scratchpad `live_smoke_*.log`,
`live_probe_silence.log`.

## 1. What GPT-Live 1 exposes

| | |
|---|---|
| model | `gpt-live-1`; also `gpt-live-transcribe` |
| endpoint | `wss://api.openai.com/v1/live/sessions` (SDK `client.live.connect()`); SIP call control under `/v1/live/sessions/{id}` (accept, reject, hangup, refer, fork, download_recording) |
| audio | one format per session, fixed at start: `audio/pcm` 24k or 16k, **`audio/pcmu` 8k**, `audio/pcma` 8k. Base64 in JSON. μ-law passes through from Twilio with no resampling |
| session config | `model, instructions, audio{format, output.voice}, delegation, input, store, client` |
| billing | $0.05/min by the second (`session.usage.updated`, cumulative snapshots); backend billed separately |
| limits | concurrent sessions by tier (25 to 500); 128k context |
| SDK | `openai>=3.8`; needs `Brotli>=1.2` and `zstandard>=0.25` or every HTTP call fails with `APIConnectionError` (hit today, root cause: vendored `httpx2` decoders). Existing importers (`resolve_ft2`, `resolve_llm`, `ft/`) verified working on 3.13.0 against Luna and Cerebras |

**Server events (22, from the SDK type union).** The callbacks we get:

```
session.started            session.closed(reason, usage)     session.updated
session.input_transcript.delta(delta, start_ms, end_ms)
session.output_transcript.delta(delta, start_ms, end_ms)
session.output_audio.delta(delta, start_ms, end_ms)         # no "done" event exists
session.delegation.created(delegation{id, target[, response_id]}, offset_ms)
session.commentary.appended / thinking.appended / instructions.appended (client_event_id, start_ms, end_ms)
session.input_audio.muted / unmuted
session.usage.updated(usage.seconds, context_window.usage_ratio)
response.event(delegation_id, event{...nested Responses event...})   # Responses mode only
error(client_event_id)     info
transport.ringing / answered / failed / dtmf.received / dtmf.send    # SIP only
```

**Client commands (11).** Everything we can send:

```
session.start(session)        session.update(session)        session.close
session.input_audio.append(audio)   session.input_audio.mute / unmute
session.instructions.append(content, delegation_id)   # steer the voice model
session.thinking.append(content, delegation_id)       # private context, not spoken
session.commentary.append(content, delegation_id)     # spoken, "may paraphrase"
response.item.create(item)    response.create          # Responses mode only
```

There is **no cancel**, no "stop speaking", no output-audio-done, no per-append
audio correlation, and no keyterm/vocabulary control. Each append is capped at
500 tokens. The SDK's typed parser returns `response.event` with the nested
`event` untyped (`event.type` is `None`); dispatch on `model_dump()["event"]`.

## 2. The two handoff provisions, measured

The live model never calls our functions itself. Backend work is handed off in
one of two ways, chosen at session start and immutable after.

### 2a. Client delegation — signal only

`session.delegation.created` fires when the model decides the turn needs the
backend. It carries an id and an offset; **no text**. We assemble the caller's
words from `input_transcript.delta`. We answer with appends keyed by the id.

Observed on the 5.6 s clip ("Yeah, this is Jordan. I can pay seventy five
dollars today, but that is all I can do right now"):

| moment | wall time |
|---|---|
| clip ends | 7.78 s |
| last transcript delta ("now") | 8.62 s |
| `delegation.created` (offset 5800 ms = exact end of speech) | 8.62 s |
| transcript already complete at that instant | yes, word-perfect over μ-law |
| our `commentary.append` | 8.62 s |
| model speaks its own filler ("Okay, Jordan. I'm checking that right now. Thanks for waiting.") | 8.8 to 12.5 s |
| appended line begins | 13.1 s |

The turn boundary is usable: it coincided with end of speech and the full
transcript was present. The cost is the 4.5 s of unsanctioned speech before
our text.

### 2b. Responses delegation — real function calling, one model removed

`delegation.created` carries `target: "responses"` and a `response_id`; a
Responses model we configure (tested: `gpt-5.6-luna`, `reasoning.effort:
none`) then runs with our tools. Observed with one tool `process_turn(utterance)`:

| moment | wall time |
|---|---|
| `delegation.created` | 8.48 s |
| nested `response.created` | 8.48 s |
| `response.function_call_arguments.done` — arguments = the borrower's exact words | 9.33 s |
| we send `function_call_output` + `response.create` | 9.33 s |
| backend text (`output_text.done`) | 10.29 s |
| voice speaks our `response_text`, verbatim, after its own filler | 11.66 s |

This is the old `process_turn` contract, reproduced: the utterance arrives as
structured arguments (extracted by the backend model from the transcript, not
by ASR), our graph returns the text, and it was spoken word for word. The price
is a second model hop of about 0.85 s to get the call and 0.96 s to return,
plus Luna's tokens, on every turn, and the voice model still paraphrases at the
mouth (§3).

### 2c. Reasoning provisions

The voice model has no reasoning knob. `delegation.responses.reasoning.effort`
and `service_tier` tune the backend only. `session.thinking.append` is the
only reasoning-side input to the voice model: private context it may draw on
later, never spoken on append. Prompt guidance from OpenAI: keep the voice
prompt short (style, backchannels, when to delegate); business rules live in
the backend.

## 3. Verbatim speech — the numbers

Four sessions, our own lines, all over μ-law:

| path | verbatim | unsanctioned speech before the result |
|---|---|---|
| `commentary.append`, mini-Miranda, no special instruction | 1/1 | 4.5 s of filler |
| `commentary.append`, money readback, with "stay completely silent, say it exactly" instruction, N=3 | **2/3** — run 2 said "I'll note seventy-five… That would leave… still past due" and dropped "after it posts" | 2/3 ("Thank you, Jordan. Let me check on that." / "Hmm.") |
| Responses delegation, money readback | 1/1 | filler ("Okay, I'm checking that right now.") |

Conclusion, on evidence and matching the docs word for word: the model is a
paraphraser and a backchanneler, and instructions reduce but do not remove
either. **No Class-A or money line may leave through the model.** The
prescribed remedy is the one we can actually implement because we own the
Twilio leg: play our own rendered audio and control the model's output while
it plays.

## 4. Turn API contract, row by row

The contract (`voice-gateway/vogon_contract/types.py`) is gateway ↔ turn
service over `process_events`. The turn service needs **no change** to run
behind GPT-Live; every adaptation is in a new gateway transport. Rows marked
CHANGE need a contract addition; rows marked LOSSY are feasible but weaker
than today; INFEASIBLE means the API cannot express it.

| Turn API item | today (Deepgram path) | under GPT-Live | verdict |
|---|---|---|---|
| `UtteranceFinal.text` | Flux end-of-turn final | assembled from `input_transcript.delta` up to `delegation.created`; in Responses mode, the tool argument | OK |
| `UtteranceFinal.confidence`, `low_conf_words` | Deepgram word confidences; feeds the STT low-confidence gate | not provided; pass `1.0, []` as the contract already allows | LOSSY (gate inert) |
| `UtteranceEager` | Flux eager end-of-turn | no equivalent; could be raised on transcript quiescence before delegation | LOSSY (optional) |
| `UtteranceFinal.heard` (HeardHorizon) | from DeliveryReport of our TTS | from our own accounting of what we relayed to Twilio, per §6 | LOSSY (approximate) |
| `UtteranceAbsorbed` / absorb verdicts | FloorController on our playout | applies only to app-played audio (input muted); model speech has no absorb | LOSSY |
| `SilenceThreshold`, `DurationThreshold` | gateway timers | unchanged, gateway timers | OK |
| `SpeakCommand.text` spoken as written | Deepgram TTS, exact | recite lines: app-played audio (exact); generate lines: `commentary.append` (paraphrase accepted) | **CHANGE**: `SpeakCommand.delivery: "recite" \| "generate"` |
| `SpeakCommand.protected` | barge cannot cut | app-played audio with `input_audio.mute` for its duration | OK for recite; model speech cannot be protected |
| `SpeakCommand.absorb_barge` | absorb during opening | same as above; caller words during the window are lost unless we run a parallel STT | LOSSY |
| `SpeakCommand.audio_id` join key | marks per playout | app-played audio: marks as today. Model speech: no audio↔append correlation exists; we correlate by output transcript text and our relay marks | LOSSY for generate lines |
| `DeliveryOutcome.status` complete/truncated/failed | DeliveryReport | app-played: as today. Model speech: derived (truncated = caller transcript overlaps our relayed audio) | LOSSY for generate lines |
| `PlayoutFinished`, `DrainCleared` | Twilio mark echoes | same mechanism: the transport still relays every frame to Twilio and can insert marks | OK |
| re-delivery of a truncated Class-A line | delivery gate | only for recite lines (app-played) | OK for the lines that matter |
| `action: end_call` | after farewell playout | `session.close` after our relay's terminal mark; `session.closed` returns final usage | OK |
| `StartCallResponse.speak` (opening, protected, absorb) | protected playout | app-played, input muted; the model is told via `thinking.append` what was said | OK |
| `silence_grace_sec`, holds | transport | unchanged | OK |
| cutting agent speech on a barge verdict | `_cut_playback` cancels TTS / clears Twilio | app-played: identical. Model speech: **no cancel command**; drop frames at the relay + `instructions.append("stop")` | LOSSY |
| STT keyterm boosting, name matcher thresholds | Deepgram keyterms | none exists | INFEASIBLE as-is; the name matcher runs on transcript text only |
| escalation (`refer`) | Twilio API | Twilio API unchanged; SIP `refer` exists but only in the SIP topology (§8) | OK |

One contract change is proposed, and it is a good one on its own: the delivery
gate today infers Class-A lines by scanning text (`detect_class_a_line`,
already flagged as a platform/domain boundary violation). `SpeakCommand.delivery`
makes the turn side, which knows whether a line was recited or generated,
say so. Default `"recite"` keeps the Deepgram transport byte-identical.

## 5. Proposed architecture

A third transport in the voice gateway, behind the existing driver. Nothing in
`data-plane/app/turns/` changes.

```
Twilio media WS ──frames──> LiveTransport(BaseTransport) ──UtteranceFinal──> driver ──process_events──> turn service
                             │                                                ▲                            │
                             ├─ SessionLink: gpt-live-1 WS (μ-law 8k, client delegation)                   │
                             ├─ UtteranceAssembler: input_transcript deltas → UtteranceFinal at delegation │
                             ├─ SpeechRouter: SpeakCommand.delivery → recite | generate   <────SpeakCommands┘
                             ├─ PlaybackController: ONE μ-law stream to Twilio from two sources
                             │     recite  → Deepgram TTS (existing TTSClient) + marks, model output dropped, input muted
                             │     generate→ commentary.append; model output_audio relayed with marks
                             ├─ BargeBridge: caller speech during app-played audio → FloorController as today;
                             │     during model speech → drop relay + instructions.append("stop")
                             └─ Lifecycle: session.start / close, usage, DurationThreshold, failure ladder
```

**One turn, client delegation (the default mode):**

1. Caller frames from Twilio are forwarded as `input_audio.append` and, while
   an app playout is not running, nothing else.
2. `input_transcript.delta` accumulates. On `delegation.created` the assembler
   emits `UtteranceFinal(text, 1.0, [], heard=<our accounting>)` onto
   `utterance_queue`. The driver builds the `ProcessEventsRequest` exactly as
   it does for Deepgram.
3. The transport immediately sends `thinking.append(delegation_id, "The backend
   is deciding. Say nothing.")`, which in testing reduces but does not remove
   filler; the real guard is step 5.
4. `process_events` returns `SpeakCommand`s.
5. For each command, `SpeechRouter`:
   - `recite`: `input_audio.mute`; drop any model `output_audio.delta` frames;
     stream Deepgram TTS of the exact text to Twilio with marks (today's
     `_do_tts_stream_sentences`, unchanged); on the terminal mark,
     `thinking.append(delegation_id, "The caller just heard, verbatim: …")`,
     then `input_audio.unmute`. DeliveryOutcome/PlayoutFinished are exact.
   - `generate`: `commentary.append(delegation_id, text)`; relay the model's
     `output_audio.delta` to Twilio with a mark per command; DeliveryOutcome is
     derived from `output_transcript.delta` text and the marks.
6. Facts spool as today and ride the next request.

Why client delegation by default: it keeps the utterance path at one hop
(0 ms extra on top of ASR) and keeps our graph the only reasoner. Responses
delegation is kept as a configuration option for the one thing it does better,
structured utterance capture with arguments, at +1.8 s per turn and a second
model in the loop.

**Opening.** `StartCallResponse.speak` is played as recite with input muted,
exactly as the protected opening is today, before the model is allowed to
speak. The voice prompt says the assistant does not greet.

**Closing.** After the farewell's terminal mark, `session.close`; wait for
`session.closed` (up to 15 s per the docs) and record `usage.seconds` into the
call analytics.

**Failure ladder.** `error`, `transport.failed`, socket loss: existing gateway
policy (trouble farewell through Deepgram TTS, which stays available) applies
unchanged; a session cannot be resumed, only forked with `store: true`, which
we do not enable in the strawman.

## 6. What is lost, plainly

- Barge-in on model speech is approximate: there is no cancel, so a verdict
  can only drop frames we have not yet relayed and ask the model to stop. The
  buffered-at-Twilio tail behaves as today (`clear`).
- The STT low-confidence gate and the measured name-matcher thresholds lose
  their inputs. Names come from the model's transcript with no boost.
- `DeliveryOutcome` for generate lines is derived, so "speak only the sentences
  they missed" is exact only for recite lines.
- Caller words spoken during a muted recite window are not heard by the model.
  Today they are absorbed and logged; here they are lost unless a parallel STT
  is run for that window (Flux is still connected on the Deepgram path; not in
  the strawman).
- Cost roughly doubles per call ($0.05/min voice on top of the same backend).

## 7. Open questions for review

1. `SpeakCommand.delivery`: accept the contract addition, and have the turn
   side stamp it from the recite/generate axis it already tracks?
2. Client delegation default, Responses delegation optional: agree?
3. The muted-window loss of caller speech: accept for the strawman, or require
   the parallel Flux STT from day one?
4. Should generate-class lines exist at all in the first milestone, or should
   every line be recite until the paraphrase rate is measured on real calls?
   Recite-only makes GPT-Live a turn-taker and backchanneler and nothing else.

## 8. Alternative topology, rejected

SIP: point a Twilio SIP trunk at OpenAI, run our backend on a sideband
(`transport.*` events, `refer` for escalation). Simpler media path, but we no
longer own the audio leg, so app-played recite audio is impossible and every
line goes through the paraphraser. Incompatible with §3.

## 9. Removal of the gpt-realtime-2 path

Deleted outright (it bypasses the Turn API and cannot import under the
templated `data-plane.service`):

```
voice-gateway/vogon/websocket/realtime_handler.py
voice-gateway/vogon/gateway/transport/openai_realtime_transport.py
data-plane/app/services/realtime_prompt.py
data-plane/app/services/realtime_tools.py
data-plane/benchmarks/realtime_vs_standard_benchmark.py
data-plane/tests/test_realtime_handler.py
data-plane/tests/sim_realtime_harness.py
data-plane/tests/simulate_realtime_collections.py
data-plane/tests/run_happy_path_realtime.py
docs/realtime-s2s-collections.md
```

Edited: `data-plane/app/main.py` (drop `/ws/twilio-realtime`),
`data-plane/app/routers/twilio.py` (drop `use_realtime` and the realtime dial
branch), `data-plane/app/config.py` (drop `openai_realtime_*`),
`frontend/twilio-demo.html` and `frontend/collections-demo.html` (drop the
toggle), `tests/test_resurface_platform_restate.py:221-267` (exec's a closure
out of the handler), `tests/test_ledger_boundary.py:37`,
`tests/test_voice_pillar_imports.py:97-99` (the standing exemption).

## 10. Milestones

| | deliverable | gate |
|---|---|---|
| M0 | removal (§9); SDK pins `openai>=3.8`, `Brotli>=1.2`, `zstandard>=0.25`; `use_live` call mode | existing suites unchanged |
| M1 | `LiveTransport` skeleton: session up over μ-law, frames relayed both ways, usage recorded | a real Twilio call hears the model |
| M2 | client-delegation turn loop: assembler → `process_events`; recite-only (every agent line via Deepgram TTS, the model's voice never relayed) | scripted call completes on the chained graph |
| M2a | demo pages: "GPT-Live" pipeline-mode radio → `use_live` → `app_payload.voice = "live"` | the toggle selects the transport |

**Implemented 2026-09-11 (M0, M2 recite-only, M2a).** The parallel path is
`voice-gateway/vogon/gateway/pipeline/live_session.py` (a drop-in for
`STTSession`: same callbacks, gpt-live-1 client delegation over μ-law) and
`voice-gateway/vogon/gateway/transport/live_transport.py` (subclasses
`TwilioTransport`, overriding only `_make_stt` and `send_response`). Existing
code touched: one extracted hook `TwilioTransport._make_stt`, one branch in
`handler._build_transport` keyed on the opaque `app_payload["voice"]`, a
`voice` kwarg on `create_gateway_call`, `use_live` on the Twilio initiate body,
four `live_*` settings, and the two demo toggles. M1's "hears the model" gate
is deliberately NOT met: with generate lines off, the caller hears only the
agent's TTS, which is the point of §3. Tests: `tests/test_live_transport.py`.
Not yet done: generate lines (`commentary.append` + relay), the barge bridge
for model speech (moot while nothing of the model's is relayed), and a real
Twilio call.
| M3 | recite playback control: mute, drop, Deepgram TTS, marks, `thinking.append` | mini-Miranda and readbacks exact on 10/10 calls |
| M4 | barge bridge and delivery facts | corpus barge signals within baseline |
| M5 | lifecycle and failure ladder | reaper and trouble-farewell tests |
| M6 | gates: eval suite N=2, corpus, campaign on `use_live` | no engine-attributed regressions |
