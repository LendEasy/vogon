# Voice Gateway — Phase 0 Design

**Status:** Approved 2026-07-21. Steps 1–7 landed; the Turn API triad (`start_call`,
`process_events`, `end_call`) is complete. Remaining work in §8.1.
**Scope:** `data-plane/` only. In-process restructure — no process split, no network protocol.
**Roadmap context:** [voice-gateway-roadmap.md](voice-gateway-roadmap.md) (all phases).
**Supersedes:** the recovery mechanism in `../../docs/call-state-recovery.md` (absorbed here as
`start_call(resume=true)`); narrows `distributed-call-architecture.md` (see roadmap §6).

---

## 1. Objective

Restructure the data plane so that the boundary between audio handling and reasoning is an
explicit request/response contract with all session state in Redis — while everything still
runs in one process. After Phase 0, the physical split (Phase 1) changes plumbing only: the
function call becomes a network call with identical types and identical semantics.

**Changed by Phase 0:** the handler's structure, the transport contract, session state
custody, the call log's home, silence/duration/filler triggering.

**Not touched:** AEC, barge gate, barge semantics, STT/TTS pipelines, ambience, agents,
planner, workflow engine, control plane, evaluator internals, deployment, the OpenAI
Realtime path (moves as files only, keeps its own seam).

## 2. Terms

| Term | Definition |
|---|---|
| **Voice Gateway** (gateway) | The stateful half. Owns the caller-facing socket, STT/TTS, AEC/barge machinery, all clocks (silence, duration, filler), and gateway memory. Knows *what physically happened and when*; never knows what anything means. |
| **Turn Service** (backend) | The stateless half. Owns planner, agents, workflow engine. Holds nothing in memory between requests; serves each request from the session store. Knows *what things mean*; never touches audio or clocks. |
| **Turn API** | The three-call request/response interface between them: `start_call`, `process_events`, `end_call`. The gateway always initiates. In Phase 0 it is a Python interface; in Phase 1, a network protocol. |
| **Event** | A physical fact the gateway reports to the backend inside a request. Ordered, gateway-timestamped. |
| **Speak command** | An instruction in a response: play this text as this `audio_id`. |
| **Session state** | The one Redis value (`call:{id}:state`) holding all meaning for a live call. The only thing the backend reads. |
| **Call log** | The one Redis list (`call:{id}:log`) of per-call events, appended at commit, drained to archive at call end. |
| **Gateway memory** | Per-call, in-process data in the gateway (delivery ledger, clocks, event spool). Never load-bearing across a request boundary. |
| **Turn scratch** | Short-TTL Redis keys for speculation staging (and, in Phase 1, retry responses). |
| **Archive** | Write-once post-call outputs: JSONL, transcript, WAV, analytics rows. Never read during a call. |
| **Commit** | The atomic Redis operation ending a request: conditional write of session state + append of log entries. State advances only here. |
| **Heard horizon** | Per-utterance snapshot: which playouts had fully played when the caller started speaking. |

The storage taxonomy is closed: gateway memory, session state, call log, turn scratch,
archive. A proposal for a sixth named place for data to live is a design smell — stop and
re-examine.

## 3. Components and storage

```
┌────────────── VOICE GATEWAY (stateful, per call) ──────────────┐
│ caller socket (Twilio / widget WS)                             │
│ STT · TTS · AEC · barge machinery          [unchanged]         │
│ gateway memory: delivery ledger, clocks, event spool           │
│ policies driven by TransportConfig: silence thresholds,        │
│   duration thresholds, filler playback                         │
└───────────────┬────────────────────────────────────────────────┘
                │ Turn API (request down, response up)
                ▼
┌────────────── TURN SERVICE (stateless) ────────────────────────┐
│ per request: load state → interpret events → plan → agents     │
│              → compose response → commit                       │
│ empty between requests                                         │
└───────┬──────────────────────────────┬─────────────────────────┘
   read + commit                write-once at end_call
        ▼                              ▼
┌── Redis (session store) ──┐   ┌── Archive ─────────────┐
│ call:{id}:state    value  │   │ JSONL log, transcript  │
│ call:{id}:log      list   │──►│ analytics DB rows      │
│ call:{id}:staged:* scratch│drain (gateway writes WAV,  │
└───────────────────────────┘   │  audio timeline)       │
                                └────────────────────────┘
```

Only two things are read during a live call: gateway memory and Redis. Everything else is
write-only output.

## 4. The Turn API

Three calls, all initiated by the gateway. Signatures are given as typed records; in
Phase 0 these are frozen Python dataclasses behind a module-level interface, in Phase 1
the same shapes over the network.

### 4.1 `start_call`

```
start_call(req: StartCallRequest) -> StartCallResponse

StartCallRequest:
  call_id:      str
  owner_id:     str
  workflow_id:  str
  mode:         "twilio" | "widget" | "text"
  resume:       bool          # reconnect to an existing session

StartCallResponse:
  transport_config: TransportConfig
  speak:            list[SpeakCommand]   # the opening
  state_version:    int
  client_events:    list[ClientEvent]    # widget frames the opening produced
```

The backend fetches config from the control plane, creates (or restores) session state, and
returns everything the gateway needs. The gateway never talks to the control plane.

`speak` is the WHOLE opening, not just a greeting: the CALL_START tools and the opening
agent run inside `start_call`, so an inbound call comes back as [greeting, opening] and a
resumed one as the pick-up-where-we-left-off line. **Decision 2026-08-01 (phone-first):**
this is one call, not two, even though it means the gateway builds its transport — and so
opens the STT/TTS sockets and starts the ambience bed — only after the opening has been
computed. On a Twilio call none of that is audible: nothing reads the media socket until
the driver's read loop starts, which is after the opening either way, and time-to-first-
audio is unchanged because the same work is serialized either way. The cost is on the
widget, where `server.session_ready` (and with it the connected indicator) now waits for
the opening. The alternative — a second turn-side call so the ready frame could go out
first — was rejected as a fourth operation bought for a browser-only symptom.

```
TransportConfig:
  persona_voice:        str
  persona_name:         str                         # who the widget names
  language:             str
  ambience:             AmbienceSettings | null
  name_hint:            str | null                  # STT keyword boost
  silence_thresholds:   list[Threshold]             # e.g. [{id:"warn",after_sec:8},
                                                    #  {id:"probe",after_sec:10},
                                                    #  {id:"exit",after_sec:37}]
  duration_thresholds:  list[Threshold]             # max-call-duration ladder
  filler:               FillerPolicy | null

Threshold:     { id: str, after_sec: float }
FillerPolicy:  { first_after_ms: float, interval_ms: float, max: int,
                 phrases: list[str] }
```

Threshold IDs are opaque labels to the gateway. It fires each once per silence window
and never knows what the backend does in response.

**Re-arm rule (decided 2026-07-21, after the §9 characterization):** a silence window
re-arms on **caller activity only**. Agent playback — including the silence probe
itself — does NOT re-arm the ladder; the window keeps counting from the original
silence onset across playouts (the elapsed clock ignores time while agent audio is
actually playing, but the anchor survives). Consequence: a fully silent call
deterministically warns, probes, and exits at the configured offsets from silence
onset. This replaces today's behavior, where exit reachability after a probe was a
race between the 1 Hz poll and the probe's Twilio mark-echo drain window
(`tests/test_silence_ladder.py` pins both branches of that race).

### 4.2 `process_events`

```
process_events(req: ProcessEventsRequest) -> ProcessEventsResponse

ProcessEventsRequest:
  call_id:     str
  request_id:  str                    # gateway-minted, unique per request
  events:      list[TransportEvent]   # occurrence order; last item is the trigger

ProcessEventsResponse:
  speak:       list[SpeakCommand]
  action:      "continue" | "end_call"
  end_reason:  str | null             # set when action == end_call
```

```
SpeakCommand:
  audio_id:   str        # minted by backend; the join key for all later physical facts
  text:       str
  protected:  bool       # play to completion; barge does not cut
```

Every event carries `seq` (gateway-assigned monotonic counter, per call) and `at`
(gateway monotonic clock). The backend treats both as opaque ordering/joining keys and
never generates physical timestamps of its own.

```
TransportEvent = one of:

  UtteranceFinal:
    text: str, confidence: float, low_conf_words: list[str]
    onset_at: float                           # when the caller started speaking
    heard: HeardHorizon
    carry_forward: AbsorbedUtterance | null   # recent absorbed barge; attached
                                              # identically to eager and final
  UtteranceEager:
    text: str, onset_at: float, heard: HeardHorizon,
    carry_forward: AbsorbedUtterance | null

  UtteranceResumed: {}                        # cancels the eager

  UtteranceAbsorbed:                          # log-only; never drives a turn
    text: str, verdict: str, source: str

  DeliveryOutcome:                            # per finished/failed playout attempt
    audio_id: str
    status: "complete" | "truncated" | "failed"
    sentences_total: int, sentences_sent: int
    heard_text: str
    first_chunk_ms: float | null, audio_ms: float | null

  PlayoutFinished:   { audio_id: str }        # terminal mark echoed; `at` = completion
  DrainCleared:      { audio_id: str }        # buffered tail cleared after complete
  SilenceThreshold:  { threshold_id: str, silent_for_sec: float }
  DurationThreshold: { threshold_id: str, elapsed_sec: float }

HeardHorizon:
  completed: list[str]        # audio_ids fully played at onset, completion order
  in_flight: str | null       # audio_id playing at onset, if any
  in_flight_heard_text: str   # heard prefix of the in-flight playout
  last_heard_text: str        # most recent fully/partially heard text (§A history
  unheard_text: str           #   anchoring — not derivable from the asks map,
                              #   which holds asks only, not all playouts)

AbsorbedUtterance: { text: str, verdict: str, at: float }
```

**Spool-and-acknowledge.** The gateway spools every fact until it has been included in a
request whose response arrived; then it drops it. Facts are re-sent on retry. This one rule
replaces the previous destructive `consume_*` reads. In Phase 0 (in-process) "response
arrived" is the function return.

**Commit vs stage.** A request whose trigger is a final utterance, a threshold, or a fact
batch **commits**. A request whose trigger is an eager utterance **stages**: the backend
computes the turn and writes the result to turn scratch without touching session state.
When the final arrives, the backend promotes the stage if the text matches, else discards
it and computes fresh. Speculation is safe by construction — the authoritative state is
never touched by a guess.

### 4.3 `end_call`

```
end_call(req: EndCallRequest) -> EndCallResponse

EndCallRequest:
  call_id:  str
  reason:   str            # hangup | silence_exit | max_duration | error | backend_directed
  transport_analytics: dict  # barge counts, STT quality, SNR — gateway end-of-call counters

EndCallResponse: {}
```

The backend runs summary/webhooks, drains the call log to the archive, writes analytics
rows (merging `transport_analytics`), and deletes the call's Redis keys.

## 5. Session store

### 5.1 Keys

| Key | Type | Written | TTL |
|---|---|---|---|
| `call:{id}:state` | JSON value | conditional write at each commit | 4 h rolling |
| `call:{id}:log` | list | append at each commit | 4 h rolling |
| `call:{id}:staged:{request_id}` | JSON value | eager staging | 60 s |
| `call:{id}:response:{request_id}` | JSON value | *reserved for Phase 1* (network retry answers) | — |

All keys use the existing `{call_id}` hash tag so one server-side script can touch them
atomically.

### 5.2 The state value

The existing checkpoint schema (`app/framework/checkpoint.py`), extended:

```
SessionState:
  schema_version:        int          # migrate-on-read across deploys;
                                      # never discard-on-mismatch
  state_version:         int          # monotonic, incremented every commit (≠ turn)
  call_id, mode
  config_pin:            { owner_id, workflow_id, resolved_config_versions }
  # — existing checkpoint fields —
  turn, identity_confirmed, active_branch_id, attempt_counts
  node_statuses:         { node_id → status }
  data_context:          DataContext serialization (fields, node_outputs, …)
  conversation_history:  last 30 turns
  # — new fields —
  asks:                  { audio_id → { node_id, text, exchange_id } }  # bounded 12,
                                                                        # append-only
  exchange_counter:      int
  pending_side_effects:  list[{ effect_id, kind, payload, status }]
```

`asks` carries no timing. Exchange resolution walks the utterance's `heard.completed`
list from most recent backward and takes the first `audio_id` present in `asks` —
physical ordering from the event, semantics from state, nothing stored twice. The
`playback_complete_t` field on `AskRecord` and the transport's `playback_complete_t()`
lookup are both deleted.

### 5.3 Commit protocol

Per committing request, one server-side script: write state **if `state_version` still
equals the value read at request start** (increment on success), append the turn's log
entries — all or nothing. On version conflict: discard the computed turn, re-read, re-serve.
Conflicts are rare by construction (the gateway holds one outstanding request per call), so
detection, not locking, is the mechanism. The 30 s advisory lock remains only for
reconnect dedup at `start_call`.

The store's current lax behaviors are inverted for authoritative mode:

- read/write errors **raise** and fail the request (no swallowing),
- the in-process fallback dict is **not used** (it is process state by another name),
- last-writer-wins is replaced by the conditional write.

**Accepted risk (decision 2026-07-21): Redis unavailability fails the call.** No fallback
path, no degraded mode. Deliberate — any half-built fallback leaks process state back in.
Revisited in a later phase (roadmap §7).

### 5.4 Call log

**Decision (2026-07-22): the call log is LIVE, per-event — not part of the atomic
commit.** Every entry streams out the moment it is appended (JSONL to disk +
`call:{id}:log` Redis Stream + `calls:monitor`, via `run_log_writer`, best-effort).
This is deliberately NOT transactional with the SessionState commit:

- Live observability is the contract: monitoring sees events as they happen, not
  at end of turn.
- Speculative and interrupted turns log too — the log records what the system
  *did*, including work that was later discarded, not only what was committed.
- Log/state atomicity is a separate concern from the voice-gateway split and is
  decoupled from it. If Phase 1 network retries ever make double-logging a real
  problem, that is its own change with its own decision (roadmap §7) — it does
  not ride in with this program.

Reasoning reads windows of the in-memory log (`log_slice`), as today. The stream
carries a 24 h TTL sealed at call end; JSONL is the archive.

## 6. One turn, end to end

```
caller speaks
     │
GATEWAY: STT final → build UtteranceFinal(+heard, +carry_forward)
         prepend spooled facts (DeliveryOutcome, PlayoutFinished, DrainCleared…)
     │
     ├──────────── process_events(request_id, events) ────────────►
     │                                            BACKEND:
     │                                              GET state (version V)
     │                                              join heard.completed × asks
     │                                                → exchange anchor
     │                                              planner → agents → mutations
     │                                              register new ask in `asks`
     │                                              COMMIT (state V→V+1, log append)
     ◄─────────── { speak:[…], action: continue } ────────────────┤
     │
GATEWAY: ack + drop spooled facts
         play speak commands (TTS, pacing, barge machinery)
         outcomes → spool, for the next request
     │
   (if the request runs long: filler policy plays a configured
    phrase on the gateway's own clock — no backend call)
```

Silence path: no caller speech → the gateway's silence window hits the `probe` threshold →
`process_events([SilenceThreshold])` → the backend builds the probe text (re-ask logic and
the consent-node probe ban, unchanged, backend-side) → the gateway plays it. The `exit`
threshold → the backend responds `action: end_call` with a farewell speak.

## 7. Mapping: every current mechanism → its Phase 0 home

| Mechanism today | Phase 0 home |
|---|---|
| `utterance_queue` + six polled transport members | the `events` list in `process_events`; all polled members deleted (`interrupted`, `seconds_since_last_speech`, `heard_horizon()`, `playback_complete_t()`, `consume_recent_absorbed()`, `consume_drain_clear()`) |
| `send_response → DeliveryReport` (return value) | `SpeakCommand` down; `DeliveryOutcome` event up in the next request |
| Ask registry + lazy `playback_complete_t` fill (`handler.py:1388`) | `asks` map in state + `heard.completed` on the event (§5.2) |
| Silence FSM in handler loop (1 Hz poll, `handler.py:2138-2247`) | thresholds in `TransportConfig`; gateway fires `SilenceThreshold` |
| Watchdog max-duration task | `duration_thresholds`; gateway fires `DurationThreshold` |
| Filler loop in handler (+ `interrupted` poll, `handler.py:1600-1657`) | `FillerPolicy` in `TransportConfig`, executed by gateway; a cut filler is a truncated `DeliveryOutcome` |
| Greeting emission | `start_call` response |
| `mark_agent_ready` private pokes, transport-side metric emission (`app/services/pipeline_metrics.py`) | timings ride in `DeliveryOutcome`; backend emits all metrics |
| `session` object in transports, full `assistant_cfg` dict | `TransportConfig`; end-of-call counters ride in `end_call.transport_analytics` |
| `barge_semantics` lazy imports of `app.agents.*` | `llm_structured_call` and the text-normalize helper move to agent-free shared modules (re-export shims at old paths) |
| Checkpoint (best-effort copy) | promoted to session state (§5) |
| In-memory `CallLog` + JSONL writer task | Redis list, drained at `end_call` |
| Speculative pipeline + mutation staging | eager request → turn scratch stage; final promotes or discards |
| Side-effect fire-and-forget tasks | recorded in `pending_side_effects` at commit; execution mechanism unchanged in Phase 0 |
| Reconnect recovery (`../../docs/call-state-recovery.md`) | `start_call(resume=true)` restores from session state — the same mechanism as normal operation, no longer a special path |

## 8. Code layout and migration order

Target layout: `vogon/gateway/` (transports, pipelines, clocks, event spool,
`TransportConfig`) and `app/turns/` (Turn API entry, planner/orchestrator wiring, session
store access). `handler.py` decomposes into the gateway-side call driver and the backend
turn function. The OpenAI Realtime files move under `vogon/gateway/` unmodified.

Ordered, independently committable steps — full simulate suite green after each:

1. **Contract types.** Events, commands, requests/responses, `TransportConfig` as frozen
   types. No behavior change.
2. **Metrics inversion.** Timings into the delivery report; delete `mark_agent_ready`
   private pokes; backend emits all metrics.
3. **Gateway-side clocks.** Silence + duration thresholds and filler policy move to the
   transport layer, driven by `TransportConfig`. Delete the 1 Hz poll and `interrupted`.
   Blocked on the silence-ladder characterization test (§9).
4. **Event-shape the facts.** Horizon and carry-forward onto events;
   `PlayoutFinished`/`DrainCleared` as pushed events; delete the remaining polled
   members and `consume_*`. (`DeliveryOutcome`-as-event moved to step 6 — it only
   becomes real when `send_response`'s synchronous return value crosses the Turn
   API; emitting it earlier would double-report with no consumer.)
5. **Session store promotion — direct cutover** (decision 2026-07-21; no shadow mode).
   Gate before the cutover commit: serialization round-trip test (state → JSON → state,
   field-complete against the runtime) and the full sim suite running on store-backed
   state. From this step onward Redis is required; local dev gets a Redis service in
   docker-compose.
6. **Handler decomposition.** Split into gateway driver + turn function behind the
   Turn API types; `DeliveryOutcome` becomes an event here (the synchronous
   `DeliveryReport` return stops crossing the seam).
   *Implementation status:* the semantic core is done — the driver spools
   physical facts (`DeliveryOutcome` synthesized at speak completion,
   `PlayoutFinished`/`DrainCleared` off the queue) and every trigger processes
   its fact batch first (§6 "facts first, trigger last", which is what makes
   the delivery interrupt correct on a barge); the delivery gate is entirely
   fact-driven; asks register before the commit, so the SessionState includes
   the turn's ask before the caller hears it. Call-log atomicity was REMOVED
   from scope — the log stays live per-event by decision (§5.4, 2026-07-22).
   *Response-shaping (with step 7 part 2b):* turn-side code composes typed
   `ProcessEventsResponse`/`SpeakCommand` values; `_execute_turn_response` is
   the driver's single execution path for all twelve former inline speak
   sites, including end_call actions (silence exit, low-confidence farewell).
   The one remaining Phase 0 item is the physical extraction of the turn-side
   functions out of `handle_call`'s closure web into `app/turns/` — a
   dedicated mechanical task now that the shapes and the harness exist. The
   extraction plan — function inventory, state custody, and boundary caveats —
   is §8.1 below.
7. **Import cuts + enforcement tests.**

### 8.1 Step-6 extraction plan (2026-07-30)

Measured against the current `handle_call` (2,922 lines, 31 nested functions).
The upward direction of the seam is typed as of `cd76746` (contract events
replace `UtteranceEvent`; `test_gateway_contract.py` pins required parameters
and zero-value defaults), so both arrows crossing the boundary below already
exist and are tested. The extraction moves function bodies behind them; it adds
no new contract surface.

#### Current structure — one closure, two allegiances

```
┌───────────────────────────────────────────────────────────────────────────────────────┐
│  vogon/websocket/handler.py — handle_call (one closure)                                 │
│                                                                                       │
│  ┌── belongs to GATEWAY driver (9) ──┐      ┌── belongs to TURN SERVICE (22) ───────┐ │
│  │  _ws_read_loop                    │      │  _process_utterance                   │ │
│  │  _send_loop                       │      │  _compose_and_commit_turn             │ │
│  │  safe_send_text / safe_send_binary│      │  _invoke_and_follow                   │ │
│  │  send_twilio_raw                  │      │  _invoke_parallel_both                │ │
│  │  _send_with_metrics               │      │  _quick_faq_query                     │ │
│  │  _execute_turn_response           │      │  _handle_eager_utterance              │ │
│  │  _deliver_turn_result             │      │  _handle_turn_resumed                 │ │
│  │  _utterance_processor             │      │  _cancel_speculative_task             │ │
│  │    (dispatch half)                │      │  _capture/_restore_cycle_anchor       │ │
│  └────────────────┬──────────────────┘      │  _serialize_state / _rebind_runtime   │ │
│                   │                         │  _process_pending_facts               │ │
│                   │  shared closure         │  _build_turn_annotations              │ │
│                   │  variables:             │  _log_end_of_turn                     │ │
│                   │  turn_counter           │  _emit_low_conf_gate                  │ │
│                   │  transcript_turns       │  _on_field_confirmed / _on_field_reset│ │
│                   │  _eager_anchor          │  _on_llm_trace                        │ │
│                   ├─── consecutive_low_conf │  _make_tool_ctx / _invoke_tools       │ │
│                   │  _ask_registry          │  _await_pending_tools                 │ │
│                   │  _metric_anchor         │  _run_call_end_tools                  │ │
│                   │  collected_all          │  _dispatch_side_effect                │ │
│                   │  _last_final ...        │  _persist_response                    │ │
│                   │                         │  _finalize_call (most of it)          │ │
│                   └────────────────────────►└───────────────────────────────────────┘ │
│                                                                                       │
│   Neither column can move until every shared variable has exactly one owner.          │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

#### Destination of each turn-side function

| Function today | Turn-service home |
|---|---|
| `_process_utterance` | body of `process_events` (utterance trigger) |
| `_compose_and_commit_turn` | commit tail of `process_events` |
| `_invoke_and_follow`, `_invoke_parallel_both`, `_quick_faq_query` | turn executor (graph stepping / planning / auxiliary reasoning) |
| `_handle_eager_utterance`, `_handle_turn_resumed` | `process_events` (eager / resume triggers); staging in turn scratch |
| `_cancel_speculative_task`, `_capture/_restore_cycle_anchor` | turn-scratch manager (promote-or-discard) |
| `_serialize_state` / `_rebind_runtime` | session-store writer / reader |
| `_process_pending_facts` | "facts first" prologue of `process_events` |
| `_build_turn_annotations` | turn-executor input assembly |
| `_log_end_of_turn`, `_emit_low_conf_gate` | call-log writer |
| STT-gate block in `_utterance_processor` | gate stage at the head of `process_events` |
| `_on_field_confirmed/_reset`, `_on_llm_trace` | turn-executor wiring |
| `_make_tool_ctx`, `_invoke_tools`, `_await_pending_tools`, `_run_call_end_tools` | tool runner (`process_events` / `end_call`) |
| `_dispatch_side_effect` | side-effect dispatcher (recorded at commit) |
| `_persist_response` | archive writer |
| `_finalize_call` | body of `end_call` (audio archiving stays gateway-side) |

The driver-side nine stay driver logic and relocate to `vogon/gateway/` with the
websocket package move. `_utterance_processor` splits: queue consumption and
batching become the driver's request assembler; everything after "parse event"
crosses the seam.

#### Closure-variable custody

The decision that unblocks the move. Each shared variable goes to exactly one
of the storage homes of §2:

| Closure variable | Custody | Reason |
|---|---|---|
| `turn_counter`, `transcript_turns`, `collected_all`, `_ask_registry`, `_exchange_counter`, `consecutive_low_conf`, `consecutive_errors`, `_last_final` | session state | meaning that survives a request boundary and a process death |
| `_eager_anchor`, `_speculative_task/_text/_gen` | turn scratch | speculation staging, promoted or discarded by the final |
| `_metric_anchor`, `_pending_facts`, filler-hold state, delivery ledger | gateway memory | physical timing, never load-bearing across a request |
| `runtime`, `planner`, `orchestrator`, `registry`, `_gate_policy` | rebuilt per request | the stateless-service property itself |

#### Target shape

```
        caller (Twilio / widget / text)
                     │ audio + frames
                     ▼
┌────────────────────────────────────────────┐
│  vogon/gateway/  — driver (stateful half)    │
│  frame loop + send loop                    │
│       │                          ▲         │
│       ▼                          │         │
│  request assembler        response executor│
└───────┬──────────────────────────▲─────────┘
        │ ProcessEventsRequest     │ ProcessEventsResponse
        ▼                          │
┌──────────────────────────────────┴─────────┐
│  app/turns/  — turn service (stateless)    │
│  start_call · process_events · end_call    │
└───────┬────────────────────────────────────┘
        │ read at request start · write at commit
        ▼
┌────────────────────────────────────────────┐
│  Redis: session state · call log · scratch │
└────────────────────────────────────────────┘
```

#### Boundary caveats (record, do not lose)

1. `_finalize_call` splits: the sweep/outcome/CALL_ENDED body is `end_call`;
   audio archiving (WAV) stays gateway-side — the recording is a gateway
   artifact.
2. `_execute_turn_response` is driver-side but currently also appends
   transcript turns (meaning) on the STT-gate paths; the extraction must move
   that write turn-side or the driver keeps a meaning-write.
3. The speculation machinery assumes the eager task and its anchor live in one
   process. Under Phase 1 that holds only with call-affinity routing (eager and
   final of one call land on the same turn-service instance). Phase 0 never
   exercises this; the constraint is recorded here so Phase 1 routing inherits
   it deliberately.

#### Execution order

Bottom-up, gates green after every stage, one commit per stable point:
(a) parameter-passable leaf functions move as module-level functions in
`app/turns/`, closure keeps thin delegations; (b) a mutable turn context gives
the custody-table state one owner and the state-custody functions move onto
it; (c) the executors move behind `start_call` / `process_events` /
`end_call`; (d) the delegations dissolve and the driver calls the Turn API
only. `tests/test_handler_harness.py` is the safety net at every stage — it
must pass unchanged.

#### Status after step (d) (2026-07-31)

`handler.py` 2,922 → 943 lines. `app/turns/api.py` holds `process_events`;
the driver's 517-line trigger loop is a 35-line assembler that batches spooled
facts with one trigger into a `ProcessEventsRequest`, makes ONE call, and
executes the response. Fourteen delegating wrappers deleted. The clock
thresholds, the STT gate, the duplicate-final guard and the utterance dispatch
are all behind the call.

Every trigger except the main utterance path is now a pure response-returner —
silence probe, silence exit, and both gate paths (re-ask, farewell) return
their `ProcessEventsResponse` instead of calling back, so
`execute_turn_response` has **zero** turn-side callers outside
`process_utterance`.

Closed since (each in its own commit):

- **`send_client_event`** — widget frames ride `ProcessEventsResponse.
  client_events` (`e8471eb`). Two documented exceptions remain by design:
  `server.thinking` (emitted by the gateway at dispatch — batching it would
  defeat the typing animation) and `server.call_ended` (finalize runs from
  paths with no response to ride on).
- **`end_call` as a function** (`353673b`) — `api.end_call` runs
  `lifecycle.finalize_call`, and the transports' `collect_analytics()` became
  `transport_analytics() -> dict` so the gateway's end-of-call counters ride
  the request as data. The two turn-side paths that finalized a call
  themselves (max-duration, error ceiling) now return `action="end_call"`;
  `finalize_call` has exactly one caller, the driver.
- **`audio_seq`** (`c1bfd95`) — `TurnContext.mint_audio_id()` owns the
  counter; it is session state, so a resumed call continues the sequence
  instead of reissuing ids the restored asks map already holds.
- **`start_call` as a function** — `lifecycle.begin_call` behind
  `api.start_call`; `handler.py` 958 → 567 lines. The driver no longer builds
  a runtime, a planner or a registry, and reads the assistant config only to
  construct the transport object itself. See §4.1 for the one-call decision.
- **Transports narrowed to call_id + TransportConfig** (`ed0a094`) — no
  CallSession, no assistant config inside vogon/gateway/. name_hint populated,
  workflow_id joins the contract, ambience rides as the full resolved
  snake_case set, stt_session_open becomes transport_analytics() data. An
  AST fence pins the constructors (import tests cannot see argument
  coupling).
- **Ordered log appends + the pre-connect config pin** (`44c4862`) — one Lua
  script takes the next per-call number AND appends (INCR+XADD, {call_id}
  hash tag), so stream order equals redis_seq order by construction once the
  turn service writes its own entries; a seq gap is a detectably lost write.
  No optimistic lock: INCR is atomic — CAS is for read-modify-write values,
  which a counter is not. Both call routes fetch + prime config BEFORE
  dialing; the harness makes any in-call control-plane fetch raise.
- **Import ledgers** (`abfbbf5`) — exact-set tests for the driver's
  reasoning-side imports and the turn side's gateway imports; entries carry
  reasons and may shrink, never grow.
- **The shared latency anchors** (`cad7293`) — `metric_anchor` and
  `agent_ready` deleted, not relocated. **Rule: no measurement spans the two
  halves' clocks.** The gateway measures response latency from its own two
  timestamps (it always knew both — the turn side was handing one of them
  back); the turn side measures its spans from its own request start; the
  agent label rides down on `SpeakCommand.agent_id` as an opaque string.
  `agent_to_tts_ms` is deleted outright (decision 2026-08-01): it was the one
  span that could only be built by subtracting across the seam, and nobody
  wanted it. `inflight_playouts` moved to `TurnContext` — the gateway held the
  dict but never touched it.

Remaining before Phase 1 (each with its reason, enumerated in
`app/turns/driver_port.py`):

1. **Verified 2026-08-02 (user): live calls run clean on the completed
   Phase 0 code** — the delivery inversion, the CallSession split, log
   custody, start_call ordering all exercised by real traffic. Still open,
   low priority: one browser-widget session (frames-on-response and the
   session_ready timing are phone-invisible), and the recording-epoch grep
   below — the epoch bug would NOT be audible on a call; it only skews the
   archived WAV timeline.
2. **(was) One Twilio call + one browser-widget session** — the changes with no
   live exercise now include the delivery inversion itself, plus start_call
   ordering, frames riding the response, session_ready after the opening.
   The Twilio call also settles the stream-epoch finding (grep
   "epoch-vs-anchor"; "no start event" = the bug is live). The 2026-07-31
   verification predates all of it.

Closed 2026-08-02, log custody + the request boundary (`710260a`): the
call-log writer (queue + drain task) is created by `begin_call`, owned by
TurnContext, closed by `api.close_call_log` — which the driver calls at
teardown AFTER awaiting the call-end tools. That ordering fixes a real drop:
finalize used to send the writer's sentinel before the tools task existed,
losing their TOOL_COMPLETED entries on the no-Redis path (teeth-verified).
New events: REQUEST_RECEIVED/REQUEST_COMPLETED bracket every process_events
call and every entry between them carries `request_id` (plain CallLog field
— requests are strictly sequential in-process); NODE_COMPLETED pairs with
NODE_INVOKED (resulting status, elapsed, mutations; status="error" on the
raise path). LLM_CALLED deliberately stays outside CallLog (user decision:
the classifier entries already carry what matters — more is noise).
DriverPort shared handles: pending_facts + recorder only.

Closed 2026-08-01, the CallSession split: no object is shared by both
halves. TurnContext owns the transcript lines, the analytics rows, the clock
origin (`call_epoch`, the same monotonic domain every event stamp uses) and
its own `call_ended` flag; the driver keeps the state machine (the socket's
lifecycle is physical) and transitions it where the turn side's transition
used to fire. The gateway's per-playout latency rows ride
`EndCallRequest.transport_analytics` (now a row LIST — per-playout rows
repeat, so a dict keyed by name could not hold them). Caller transcript
lines are stamped at utterance ONSET on the shared clock, not at processing
time (user direction); the assistant lines stamp at compose. Found dead on
the way: `session.conversation_history` had no reader but the anchor
snapshot, `session.graph` none at all, and the transcript header formatted a
MONOTONIC start time as a unix date — `started_at_utc` fixes it. An AST
fence pins `.session` out of app/turns/.
2. **Renumber note:** the remaining list above is now two items + the
   verification.

Closed 2026-08-01, the delivery inversion: EVERY trigger returns its
response — the main reply, the error apologies and the endings ride
`ProcessEventsResponse` (compose folds invalidation and sets
action/end_reason); the escalation warm transfer runs inside `end_call`
(reason == "escalated", CallSid as `EndCallRequest.provider_call_id`), the
same play → transfer → teardown order the driver used to enforce by hand.
DriverPort callables are down to begin_hold/end_hold (kept BY DESIGN: the
barged-filler discard must happen between compute and compose so a discarded
turn commits nothing — the Phase 1 shape is gateway-local holds plus a
discard protocol, a recorded decision) and send_call_ended. Deliberate
deltas: an invalidated turn's final line is now barge-protected and its
playout registered as finalizing; the transient-apology error frame precedes
the apology audio; a compose failure on the speculative-reuse path now gets
the same error policy as the full path.

Dropped, with reasons recorded: the `vogon/websocket/` → `vogon/gateway/` file
relocation (cosmetic — the real coupling was the constructor arguments,
closed by `ed0a094`).

## 9. Testing and enforcement

- **Import-boundary test:** no module under `vogon/gateway/` imports `app.agents`,
  `app.framework`, or `app.turns`; no module under `app/turns/` imports `vogon.gateway`
  internals beyond the contract types.
- **No-private-access test:** backend code contains no `transport._*` references.
- **Contract-only fake gateway:** the sim suite drives the Turn API directly with scripted
  event lists — sims become contract tests of exactly the surface Phase 1 puts on the
  wire. Per the repo rule, sim updates land in the same commits as the seam changes they
  track.
- **Serialization round-trip test** (gates step 5): every field the turn function reads
  survives state → JSON → state.
- **Silence-ladder characterization test** — DONE (`tests/test_silence_ladder.py`,
  commit `a7b443c`). Verdict: exit reachability after a probe was a race between the
  1 Hz poll and the probe's mark-echo drain window (tick observes `waiting=None` →
  full ladder reset → probe loop, exit unreachable; mark echoes first → anchor
  survives → exit at 37 s from onset). Resolved by the §4.1 re-arm rule:
  caller activity only.

## 10. Out of scope

Process split, network transport, retry/response-cache activation, side-effect execution
redesign (recorded in state; mechanism unchanged), per-persona threshold values from the
control plane (the `TransportConfig` slot exists; values come from settings), the OpenAI
Realtime seam, and any change to AEC/barge/STT/TTS behavior.

**Deferred decision register:** Redis-down call failure (accepted; revisit later),
side-effect executor, response cache (Phase 1), realtime-path integration (Phase 1+).

## 11. Decisions record (2026-07-21)

1. Redis rollout: **direct cutover**, gated by round-trip test + store-backed sim run.
2. OpenAI Realtime path: **untouched** (files move only).
3. Fillers: **gateway policy from day one**.
4. Layout: **new packages** `vogon/gateway/` + `app/turns/`.
5. **Redis unavailability = call failure.** Accepted risk, no mitigation built in Phase 0.
