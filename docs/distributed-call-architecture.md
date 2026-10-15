<!-- Copied 2026-10-11 from the application's monorepo (data-plane/docs/distributed-call-architecture.md) because the gateway's code cites it.
     It is a design record: paths and modules it names are the application's unless they say vogon/. -->

# Distributed, Event-Driven Architecture for Reliable Real-Time Voice Calls

**Status:** Design proposal
**Scope:** `data-plane/` live-call pipeline
**Audience:** platform / infra decision-makers
**Location note:** placed in `data-plane/docs/` alongside the four existing design docs
(`intent_routing_config_design.md`, `loan_adjustment_agent_design.md`, etc.), which is the
established design-doc home for the data-plane. (The compliance design lives under
`data-plane/evals/`; that directory is eval-specific, so `docs/` is the better fit here.)

---

## 1. Executive summary and the core tension

Today a live call is handled end-to-end inside **one coroutine** in **one uvicorn process**
(`vogon/websocket/handler.py::handle_call`, run with `uvicorn --workers 1` on a single
non-autoscaled `t3.small`). Speech-to-text finals, the routing decision
(`Planner.plan`), every subagent (`.handle`), text-to-speech streaming, silence timers,
and filler speech all run in that same event loop. If the process dies mid-call, the call
is lost. The only durable safety net is a per-turn Redis checkpoint
(`call:{call_id}:checkpoint`, 4h TTL) that lets a *reconnecting* call resume conversational
state — it does not keep the current call alive.

The goal is to make this **event-driven and distributed**: when something happens, emit a
durable event, and a consumer picks it up and processes it — so a single crash does not lose
in-flight work, and load spreads across a worker fleet.

**The core tension.** Two connections *cannot* be made stateless or brokered without
destroying call quality:

- **The Twilio Media Streams WebSocket** — the bidirectional μ-law 8 kHz audio path
  (`app/transport/twilio_transport.py::TwilioTransport`). It is a persistent per-call socket
  established when Twilio dials the `<Stream url=...>` in `routers/twilio.py::twiml_webhook`.
- **The Deepgram TTS WebSocket** — one persistent streaming connection per call
  (`app/pipeline/tts.py::TTSClient.open()`, "persistent WebSocket connection per call rather
  than one HTTP request … ~18ms avg vs ~69ms on REST, and clean barge-in via
  Clear/Cleared").

These two sockets are **stateful, sticky, and latency-critical**. The *logic* (deciding and
generating text) is stateless and parallelizable. The whole design is about splitting these
two cleanly.

**Recommended architecture (2–3 sentences).** Keep a **thin stateful "gateway" tier** that
owns both persistent WebSockets and enforces per-call turn ordering, and move the
**stateless logic tier** (decider + subagents) either in-process on the hot path or onto a
**gRPC worker pool keyed by `call_id`** for isolation — never behind a broker on the turn
path. Route **async, non-blocking events** (side-effects, call logging, recovery snapshots,
post-call jobs) through the **existing, retained Redis-based worker queue** (`call_end_jobs`
consumer group + PEL/`XAUTOCLAIM`/DLQ) — a fixed component the user intends to keep — reserving
managed AWS transports only as *optional augmentation* for a specific durability gap Redis
cannot meet (and MSK only when a replayable cross-service backbone is actually needed). This is
distinct from — and should not be conflated with — adopting **Temporal (or Step Functions) for
call-level durability and durable timers** —
silence and filler timers, barge-in signals, and the checkpoint-recovery pattern are exactly
what those engines exist for. Be honest about the asymmetry: **distributing the logic protects
a call from a *worker* crash, not from a *gateway* crash** — because the gateway owns the two
irreplaceable audio sockets.

---

## 2. Current-state analysis (grounded in the code)

### 2.1 The per-turn hot path today

All of the following happens inside `handle_call`'s event loop for one process:

1. **STT** — `app/pipeline/stt.py::STTSession` streams the Twilio μ-law audio to Deepgram
   (Flux model, v2 `TurnInfo` event stream). It emits `EagerEndOfTurn` (speculative),
   `EndOfTurn` (final), and `TurnResumed`. Finals land on `transport.utterance_queue`.
2. **Utterance dispatch** — `_utterance_processor()` consumes that queue one item at a time
   under an `asyncio.Lock` (`utterance_lock`), guaranteeing per-call turn serialization.
   `turn_counter[0]` is the monotonic turn number.
3. **Decide** — `_process_utterance` → `_compute` calls
   `planner.plan(_llm_utterance, call_history)` via `loop.run_in_executor(None, …)`. `plan()`
   is a **blocking Cerebras `llm_structured_call`** producing a `MultiIntentLLMResponse`; the
   orchestrator (`app/framework/orchestrator.py::TurnOrchestrator.build_proposal`) turns
   intents into `invoke_steps` / `reset_steps` (a `StateDeltaProposal`).
4. **Act** — each step runs through `_invoke_and_follow`, which calls
   `agent.handle(utterance, node_def, agent_state, data_context, log_slice, config)` (also in
   an executor). Multiple invoke steps run **concurrently** via `asyncio.gather`. Agent
   outputs are mutations applied through the single entry point
   `runtime.apply_mutations(result.mutations, agent_id=…)`.
5. **Speak** — assembled speak text is deduped (`apply_dedup_filter`) and streamed:
   `transport.send_response(speak_text, audio_id)` →
   `TwilioTransport._do_tts_stream` pulls chunks from the persistent Deepgram TTS WS,
   converts to μ-law 160-byte frames, and pushes them onto a per-call
   `asyncio.PriorityQueue` (`_send_queue`, 0=audio, 1=text) drained by `_send_loop`.

The latency-critical chain is **STT final → Planner → subagent(s) → TTS**, matching
`CLAUDE.md`'s stated ordering. Two latency-hiding mechanisms already exist and are important
for the distributed design:

- **Eager/speculative execution.** On `EagerEndOfTurn`, `_handle_eager_utterance` starts a
  speculative `_process_utterance(deliver=False)` task. On the real `EndOfTurn`, if the text
  matches, the speculative `_TurnResult` is reused; otherwise it is cancelled and recomputed.
  Staleness is guarded by **monotonic generation counters** `_eager_gen` / `_speculative_gen`
  — a result whose generation no longer matches is discarded. *This is already a
  fencing-token pattern* and is directly reusable for distributed staleness control (§6).
- **Empathy fillers.** If `_compute` has not finished 1100 ms after start, the handler speaks
  generic filler phrases (`app/pipeline/fillers.py::filler_sequence`, capped at `MAX_FILLERS=3`)
  to cover LLM latency, cancelling on barge-in.

### 2.2 Ancillary real-time events today

- **Silence detection** is an **in-process asyncio timer** inside `_utterance_processor`: a
  1-second `wait_for` timeout on the utterance queue computes `seconds_since_last_speech` and
  escalates through `warn` (8 s) → `alert` (10 s, fires a spoken probe re-asking the last
  question) → `exit` (hang up). Thresholds: `_SILENCE_WARN_SEC`, `_SILENCE_ALERT_SEC`.
- **Barge-in** is handled at the **transport layer**, below the agent: `TwilioTransport`
  monitors inbound RMS against `_BARGE_IN_RMS_THRESHOLD` during TTS playback (with AEC echo
  suppression), sets `_tts_cancel`, sends Deepgram `Clear` (`TTSClient.cancel()`), and issues a
  Twilio `clear` frame to flush buffered audio. `TURN_RESUMED` cancels speculative work.
- **TTS error recovery** — `send_response` distinguishes pre-first-chunk disconnect
  (`chunks_sent == 0`, silent retry) from mid-stream disconnect (re-speak prefixed with
  "Sorry about that."), per `CLAUDE.md`.

### 2.3 What is already durable / distributed

The codebase is **already partway** to the target — this is the migration's foundation:

- **Per-turn checkpoints.** `app/framework/checkpoint.py::serialize_checkpoint` produces a
  workflow-agnostic dict (turn, node statuses, full `DataContext` incl. `node_outputs`,
  capped 30-turn history). `app/services/checkpoint_store.py` writes it to Redis
  (`call:{call_id}:checkpoint`, `SET ex=14400`) fire-and-forget per turn, with an in-process
  fallback. **Recovery already works**: `handle_call` lines ~167–202 load the checkpoint on a
  reconnect, `restore_runtime_from_checkpoint`, rebind runtime/graph, and set
  `skip_opening=True`.
- **Statelessness by contract.** Per `CLAUDE.md`, every subagent must produce correct behavior
  from empty `internal_state`, reading cross-turn state from
  `data_context.get_node_output(node_id, key)` and writing it via `NodeOutputSet`. **This is
  precisely the property that makes subagents safe to run on a distributed, stateless worker.**
- **A Redis-based background worker queue is already in production — and is a FIXED,
  RETAINED component of this design.** `app/worker/job_processor.py` is a **separate process**
  consuming the Redis Stream `call_end_jobs` with consumer group `workers`: `XREADGROUP`, PEL
  drain, `XAUTOCLAIM` of messages idle > 5 min, dead-letter to `call_end_jobs:dead` after 3
  deliveries. This is not merely "proof the team can run durable consumers" — it is the
  **designated async/background transport going forward.** The user intends to keep it. This
  design therefore **builds the async/side-effect tier on top of this Redis worker queue**, and
  does *not* propose replacing it with SQS/EventBridge/MSK. Any managed-AWS alternative appears
  below only as an **optional augmentation** for a specific durability gap Redis cannot meet
  (named explicitly in §5.3), never as a wholesale swap.
- **A GPU logic service already exists off-box.** The shadow classifier
  (`app/agents/shadow_classifier.py`) can run `CLASSIFIER_BACKEND=service`, an **HTTP call to
  a separate GPU classifier host** (`aws_instance.classifier`, `g6.xlarge`, Gemma-3-4B /
  ModernBERT-NLI, port 8002). So "run the decider off the call process" is not hypothetical —
  a decider RPC out of the box is an *extension of a pattern already in flight*.
- **Async side-effects.** `SideEffectRequested` / `SideEffectCompleted` mutations are already
  dispatched fire-and-forget via `asyncio.create_task(_dispatch_side_effect(mut))` and
  reconciled back into the runtime — natural first candidates for the bus.

### 2.4 Reliability gaps

| Gap | Evidence | Consequence |
|---|---|---|
| Whole call in one process | `handle_call` single coroutine, `--workers 1`, single `t3.small`, no ASG | Process crash / deploy / instance loss drops **every** in-flight call |
| Silence + filler timers are in-process | `_utterance_processor` asyncio timers, `fillers.py` | Die with the process; no durable "check on the caller" guarantee |
| No session routing / affinity infra | single EIP, no ALB/ASG; `session_manager.py` is a 15-line shim, no `active_sessions` registry | Cannot place a call on a chosen worker or fan calls across a fleet |
| Decider blocks the loop | `planner.plan` in an executor thread, Cerebras 380–530 ms median (per memory) | One slow LLM call stalls that call's turn; no isolation or independent scaling |
| Recovery preserves *state*, not *audio* | checkpoint recovery needs Twilio to re-establish `<Stream>` | A gateway crash still interrupts the caller; recovery is best-effort reconnect |
| Post-call worker undeployed | no systemd unit for `worker.py` in `user_data.sh.tpl` | Durable job pipeline exists but is not wired in prod |
| Redis/S3 opt-in at runtime | `user_data.sh.tpl` doesn't set `REDIS_ENABLED`/`S3_ENABLED` | Durability silently degrades to in-process dict |

### 2.5 Current AWS footprint (what we build on)

Single-region `us-west-2`, **default VPC**. `aws_instance.codelexin` (`t3.small`, one EIP, no
ASG); `aws_instance.classifier` (`g6.xlarge` **spot**, GPU logic on :8002);
`aws_elasticache_serverless_cache.redis` (**Redis 7, cluster-mode, TLS**, used for
checkpoints, per-call log stream `call:{call_id}:log`, global `calls:monitor` stream, and the
`call_end_jobs` queue); two S3 buckets (`codelexin-call-artifacts` + logs, Glacier-IR at 90 d);
`aws_db_instance.codelexin` (**Postgres 16**). **In production, Postgres is the store for
everything** (call records, gathered parameters, analytics, control-plane config) **except a
subset of call metrics that are written to local disk** on the box (the `storage/metrics`
CSV/tags sink). **No MSK, no SQS, no Step Functions, no ALB/ASG today**, and — importantly —
**a Redis-based background worker queue is already in production and is a retained, fixed
component of this design** (see §2.3). Everything net-new below is additive to this footprint.

---

## 3. Event taxonomy

The single most important design decision is **which events cross a broker and which do not**.
The user's hop budget is **20–100 ms of added latency acceptable on the hot path** — enough
for one intra-VPC RPC, **not** enough for a produce-then-poll broker round-trip on every turn.

Idempotency key throughout: **`(call_id, turn)`** (from `turn_counter`), plus `audio_id`
(`resp-N`) for TTS. `call_id` is also the **partition/affinity key** for all per-call ordering.

| Event | Trigger (code) | Hot-path? | Ordering | Delivery | Idempotency | Broker hop OK in 20–100 ms? |
|---|---|---|---|---|---|---|
| **STT turn-final** | `STTSession` `EndOfTurn` → `utterance_queue` | **Hot** | Strict per-call | At-most-once (audio is ephemeral) | `(call_id, turn)` | **No** — stays in gateway; never brokered |
| **STT eager-EOT** | `EagerEndOfTurn` → `_handle_eager_utterance` | **Hot (speculative)** | Per-call, supersedable | At-most-once | `(call_id, eager_gen)` | No — gateway-local |
| **Plan / decide** | `planner.plan` | **Hot** | Per-call, serial | Effectively-once (retry on failure) | `(call_id, turn)` | **Marginal — direct gRPC only**, never reply-topic |
| **Subagent invoke** | `agent.handle` via `_invoke_and_follow` | **Hot** | Per-call; parallel *within* a turn | Effectively-once | `(call_id, turn, agent_id)` | **Marginal — direct gRPC only** |
| **TTS speak** | `transport.send_response` | **Hot** | Strict frame order | At-most-once | `audio_id` | **No** — must stay on the persistent TTS WS |
| **Barge-in / cancel** | RMS threshold → `_tts_cancel`, `TURN_RESUMED` | **Hot** | Must preempt in-flight work | At-most-once, must not be lost | `(call_id, turn/gen)` | No — gateway-local; propagated to workers via cancel/fence |
| **Silence timeout** | `_utterance_processor` timer (8/10 s) | **Near-real-time** | Per-call | At-least-once (must fire) | `(call_id, silence_window)` | **Yes** — a durable timer is *better* than today |
| **Filler speech** | 1100 ms elapsed → `filler_sequence` | **Near-real-time** | Per-call | At-most-once (cosmetic) | `(call_id, turn, filler_n)` | Yes for the *trigger*; the *audio* stays on the TTS WS |
| **Side-effect** (e.g. send_payment_link) | `SideEffectRequested` | **Async** | None | At-least-once | `effect_id` | Yes |
| **Recovery snapshot** | `serialize_checkpoint` per turn | **Async** | Last-writer-wins | At-least-once (idempotent overwrite) | `call_id` (single key) | Yes — already async |
| **Call-log append** | `run_log_writer` → JSONL + `XADD call:{id}:log` + `calls:monitor` | **Async** | Per-call append order | At-least-once | `(call_id, seq)` | Yes — already on Redis Streams |
| **Post-call job** | `_finalize_call` → `XADD call_end_jobs` | **Async** | None | At-least-once + DLQ | `call_id` | Yes — already implemented |

**Reading of the three user ideas against this table:**

1. **Subagent-per-event → worker fleet.** *Viable, high value.* Subagents are already
   stateless-by-contract, so they run correctly on a stateless worker given `DataContext` +
   `log_slice`. But because they are hot-path, the transport must be **direct gRPC to a
   worker**, not a broker; and because multiple subagents run concurrently *within a turn*,
   the fan-out/fan-in (`asyncio.gather`) stays orchestrated by the gateway.
2. **Decider → worker fleet.** *Viable, and the closest to something already built* (the
   `:8002` classifier service). Same constraint: hot-path ⇒ direct RPC, not a broker.
3. **Silence / filler as events.** *This is where an event/timer engine shines* — these are
   near-real-time, must-fire, and currently die with the process. **Durable timers (Temporal)
   or scheduled events are a strict reliability upgrade here**, with the caveat that the
   *filler audio itself* still has to be spoken on the gateway's persistent TTS WS.

---

## 4. Options comparison

Every option converges on the same physical shape — **a stateful gateway owns the two
sockets; logic is stateless** — and differs only in *how logic and async events are
transported and made durable*. That shared shape:

```
        Twilio PSTN                          Deepgram STT WS   Deepgram TTS WS
             │  (persistent media WS)              │  (persistent, per call)
             ▼                                     ▼        ▲
   ┌───────────────────────────────────────────────────────────────┐
   │  GATEWAY (stateful, sticky, one process owns THIS call)        │
   │  • owns Twilio WS + Deepgram TTS WS  • utterance_lock / turn#  │
   │  • fences results by (call_id, turn/gen)  • speaks all audio   │
   └───────┬───────────────────────────────────────────┬───────────┘
           │ hot path (direct RPC, 20–100 ms)           │ async (durable bus)
           ▼                                            ▼
   ┌───────────────────────┐                 ┌──────────────────────────────┐
   │ LOGIC WORKERS         │                 │ DURABLE EVENTS               │
   │ decider + subagents   │                 │ side-effects, checkpoints,   │
   │ (stateless, scalable) │                 │ call-log, post-call, timers  │
   └───────────────────────┘                 └──────────────────────────────┘
```

The sockets **never traverse a broker or a worker**. That single rule resolves the tension and
answers three of the task's questions at once:

- *Ordering of TTS chunks* — chunks are generated by the gateway's own TTS WS and pushed onto
  its `_send_queue`; they never cross the bus, so **they cannot be reordered by the
  distribution layer**. Ordering is purely a gateway concern (fence worker results by
  `(call_id, turn)` before feeding TTS).
- *How a decision returns with sub-100 ms latency* — it returns over the **same direct RPC
  channel** the gateway called out on (a synchronous gRPC response), not via a reply topic.
- *Barge-in to in-flight distributed work* — the gateway cancels the gRPC call / sends a
  Temporal signal and **bumps the generation counter**; any late worker result is discarded by
  the existing `_eager_gen`/`_speculative_gen` fence.

### Option A — Kafka / MSK event bus with dedicated worker fleets (anchor option)

**Shape.** An MSK (or MSK Serverless) cluster is the durable backbone. Topics:
`call.turn.final`, `call.decide`, `call.subagent`, `call.sideeffect`, `call.log`,
`call.recovery`, `call.postcall`. **All topics keyed by `call_id`** so a call's events land on
one partition and preserve per-call order. Consumer groups per event class:
`decider-workers`, `subagent-workers`, `timer-workers`, `sideeffect-workers`. The gateway
(edge process owning the two sockets) is a Kafka **producer** for async topics and a
**consumer** of a per-gateway reply.

**The critical design choice — do NOT put the hot path on Kafka reply topics.** Producing a
`call.decide` event and polling a `call.decide.reply` topic adds producer batching (linger),
partition assignment, and consumer poll jitter — easily 50–200 ms of *variance*, and voice is
judged on **p99, not median**. Every turn is user-perceived. The correct form:

- **Async backbone on Kafka** (side-effects, call-log, recovery, post-call, cross-service
  fan-out, replay/audit). This is where MSK earns its keep: durable, replayable, multi-consumer.
- **Hot path via direct gRPC** from the gateway to a `call_id`-affinitized decider/subagent
  worker pool. The Cerebras call (`planner.plan`) and `agent.handle` simply *move* from the
  gateway's executor thread to a worker; the marginal added latency is one intra-VPC RPC
  (~1–5 ms network + serialization) — comfortably inside 20–100 ms.

**Coexistence with the two persistent sockets.** The gateway process keeps the Twilio and TTS
WebSockets. Session affinity (`call_id → gateway`) is established at the TwiML step: when
`routers/twilio.py::twiml_webhook` issues `<Stream url=...>`, it can bake the chosen gateway's
address (or a sticky routing key) into the URL, so Twilio dials **that** gateway. Workers are
stateless; they receive `DataContext` + `log_slice` in the RPC and return mutations/speak. If
a worker dies mid-RPC, the gateway retries on another worker or falls back to in-gateway
compute (the call survives). If the gateway dies, both sockets drop — see the failure matrix
in §6.

**Latency profile.** Async topics: tens of ms to seconds (irrelevant, off-path). Hot path:
gРPC ~1–5 ms + Cerebras 380–530 ms (unchanged; it dominates). **Verdict: use MSK for the async
backbone; keep the decision path off it.**

**Reliability / delivery.** Kafka gives at-least-once with per-partition ordering and replay —
excellent for audit/log/recovery/post-call. Exactly-once (idempotent producer + transactions)
is available but unnecessary given `(call_id, turn)` idempotency keys.

**Ops burden.** Highest of the options. MSK Serverless removes broker sizing but still needs
IAM/SASL, topic management, consumer-group monitoring, and a schema discipline. **New
infra**: MSK + a gRPC worker fleet + a load balancer / affinity layer that does not exist today.

**AWS-native fit.** MSK / MSK Serverless are native. Good.

**When to choose A:** when a **replayable, multi-consumer cross-service backbone** is genuinely
needed (analytics, multiple downstream systems, event sourcing). It is an *evolution target*,
not the starting point — it is overkill for the current single-service, single-region system.

### Option B — Temporal (call-as-workflow)

**Shape.** Each call is a **Temporal Workflow** (`CallWorkflow`, keyed by `call_id`). The
**gateway is a Temporal Worker that owns the two sockets** and hosts the call workflow's
socket-bound activities. Per-turn decider and subagents are **Activities**
(`DecideActivity`, `SubagentActivity`) — stateless, retried, timed out. Silence and filler are
**durable timers** (`workflow.sleep` / timer futures). Barge-in and `TURN_RESUMED` are
**Signals** into the workflow. The workflow's event history *is* the recovery log — it
**replaces the hand-rolled `serialize_checkpoint` + Redis checkpoint recovery** entirely:
Temporal replays the workflow to reconstruct state after a worker crash.

**Why it fits this system unusually well:**

- Durable timers map **exactly** to the silence ladder (8 s / 10 s) and the 1100 ms filler
  timer — the very events that today die with the process (§2.4). This is the single highest
  reliability leverage per unit of effort.
- Signals map to barge-in / `TURN_RESUMED` cleanly, with the workflow deciding whether to
  cancel the in-flight decide activity.
- The `node_outputs`/`DataContext` + statelessness-by-contract design already matches
  Temporal's "activities are stateless, state lives in workflow history" model.

**The voice-latency caveat (be honest).** Temporal **Activity task-queue scheduling** adds
poll/dispatch overhead (typically low-tens of ms, but *variable*). That is fine for a
call-level workflow and for timers/signals, but running **every micro per-turn decision**
through activity dispatch risks p99 spikes on the hot path. **Map Temporal to
call/turn-level durability, timers, and signals — and keep the innermost decider/subagent hop
as a direct call** (a "local activity" or an in-worker function) rather than a fully scheduled
activity when latency matters.

**Latency / reliability / ops.** Reliability: best-in-class durable execution and recovery.
Ops: needs Temporal — **self-hosted on EC2/EKS** (there is no AWS-managed Temporal; Temporal
Cloud is third-party, mentioned only for comparison). That is real operational weight, but it
*removes* the bespoke checkpoint/recovery code and the in-process timer fragility.

**AWS-native fit.** Not native (self-host or Temporal Cloud). This is the main strike against
it under the managed-AWS preference — weigh the durable-timer/recovery win against running
Temporal yourself.

### Option C — AWS Step Functions (+ EventBridge / SQS)

**Shape.** A Step Functions **Express** state machine per call or per turn; EventBridge for
event routing; SQS for durable async work. Decider/subagents as Lambda or ECS tasks invoked by
the state machine; silence/filler as `Wait` states or EventBridge Scheduler.

**Tradeoffs at voice latency.** Step Functions state transitions and Lambda cold/warm dispatch
add **tens of ms to sub-second**, and EventBridge is **~sub-second** end-to-end — acceptable
for **call-level and post-call orchestration**, but **too slow and too jittery for the
per-turn hot path**. Standard Workflows bill per state transition (fine at 100–1k calls);
Express is cheaper/faster but still not a sub-100 ms turn engine.

**Where it fits.** Its strongest, clearly-in-scope use is **call-level lifecycle orchestration
and durable silence/callback timers** (via EventBridge Scheduler) — the same niche as Temporal,
evaluated in §5.3. Note it could *also* express the post-call pipeline (S3 → summary → webhook)
as a state machine, **but that pipeline already runs on the retained Redis worker queue and is
not being replaced** — so treat post-call-on-Step-Functions as a non-goal here, not a
recommendation. **Do not put the decider on the turn path here.** Lower reliability ceiling than
Temporal for fine-grained mid-call state, but far lower ops burden and fully AWS-native.

### Option D — Lighter buses: Redis Streams / NATS / SQS+SNS

- **Redis Streams — already in production here** (`call_end_jobs`, `call:{id}:log`,
  `calls:monitor`) on the existing ElastiCache Serverless cluster. **Lowest ops burden by far**
  — zero new infra. Consumer groups + PEL + `XAUTOCLAIM` + DLQ already implemented in
  `job_processor.py`. Good enough for **all async events** at 100–1k calls. Weaker than
  Kafka/MSK on: long retention/replay, very high fan-out, and multi-consumer independence — but
  none of those are required today. **This is the pragmatic async transport.**
- **NATS (JetStream)** — the best **request/reply latency** of any option (sub-ms), which makes
  it attractive for the hot-path decider RPC. But it is **not AWS-managed** (self-host),
  so it loses on the managed-AWS preference; direct gRPC gives comparable latency without a new
  system.
- **SQS + SNS** — fully managed, dead-simple, at-least-once with a native DLQ. **Async-only**
  (no ordering except FIFO queues, no low-latency request/reply). Not proposed here as the
  async transport (the Redis worker queue is retained); relevant only as the *optional
  augmentation* target in §5.3 if a cross-AZ-durability gap must be closed.

### Hybrid (the shape the constraints actually point to)

- **Hot path:** in-gateway compute *or* direct gRPC to a `call_id`-affinitized decider/subagent
  worker pool. No broker.
- **Async events / background work:** the **existing Redis-based worker queue, retained** — new
  async side-effects route through it, not a new system. MSK/SQS considered only as optional
  augmentation for a named durability gap, never a replacement.
- **Call-level durability + timers + barge-in signals:** a *separate* concern from the general
  background queue — Temporal *or* Step Functions, whichever the team will actually operate.
  This is the biggest single reliability upgrade.

---

## 5. Recommendation

For **100–1000 concurrent calls, managed-AWS-native, with the two persistent sockets
preserved**, adopt the **hybrid, in three tiers**:

### 5.1 Gateway tier (stateful, sticky) — the socket owner

- Run the existing `handle_call` logic as a **horizontally-scaled gateway fleet** behind an
  **ASG** (replacing the single `t3.small`), each instance owning a subset of calls. A gateway
  instance holds, per call, the **Twilio media WS** and the **Deepgram TTS WS**, the
  `utterance_lock`, `turn_counter`, `_send_queue`, and the barge-in/silence/filler machinery.
- **Session affinity via `call_id → gateway`, established at TwiML issuance.**
  `routers/twilio.py::twiml_webhook` selects a healthy gateway (least-loaded) and emits
  `<Stream url="wss://<gateway-host>/ws/twilio">` (the default `TwilioTransport`/`handle_call`
  path; `/ws/twilio-realtime` is the separate OpenAI-realtime path). Twilio then connects that
  call directly to that gateway. Note that **Twilio strips query parameters from the WS URL**
  (see the comment in `twiml_webhook`), so affinity must be encoded in the *host* (a
  per-gateway hostname) — not a query-string routing key. This is **net-new routing infra**
  (today there is one EIP and no LB) — a Network Load Balancer with connection stickiness, or
  direct per-gateway hostnames chosen at TwiML time.
- **Sizing.** At ~1–2 vCPU per active call (the loop is mostly I/O-bound; the heavy Cerebras
  work moves to workers), a `c-family` gateway hosts ~50–100 calls. 1000 concurrent calls ⇒
  ~10–20 gateways + headroom. The gateway is deliberately **thin**: sockets + ordering + audio,
  no heavy compute.

### 5.2 Logic tier (stateless) — decider + subagents

- Move `planner.plan` and `agent.handle` behind a **gRPC worker pool** (`DecideService`,
  `SubagentService`), invoked **directly** by the gateway with `call_id` affinity for cache
  locality. This is a natural extension of the existing `CLASSIFIER_BACKEND=service` pattern —
  the `:8002` GPU host already answers *classification* RPCs, today in *shadow* mode only
  (`CLASSIFIER_MODE` defaults `off`; even `service` mode is classification-only and "not yet
  wired for routing"). The analogy is the transport shape, not a claim that decisions already
  route off-box. Workers are stateless: they receive the
  serialized `DataContext` + `log_slice` + config and return mutations + speak. **Fan-out/fan-in
  for multi-subagent turns stays in the gateway** (`asyncio.gather` over per-subagent RPCs).
- **Phase this in:** it is fully optional at first — the gateway can keep computing in-process
  and flip to RPC per-workflow behind a flag. The hop budget (20–100 ms) is met because the
  transport is a single intra-VPC gRPC call, not a broker.
- Worker fleet sizing tracks Cerebras concurrency, not call count: size to peak *simultaneous
  in-flight decisions* (≈ turns/sec × decide-latency), typically far fewer than 1000.

### 5.3 Async-events tier (retained) + call-durability tier (new)

- **Async events stay on the existing Redis-based worker queue — this is a fixed decision, not
  a migration target.** New async work (side-effects `SideEffectRequested`, call-log fan-out,
  recovery snapshots, and additional post-call jobs) routes through the same `call_end_jobs`-style
  Redis Stream + consumer-group pattern already proven in `job_processor.py` (PEL, `XAUTOCLAIM`
  reclaim, dead-letter after 3 deliveries). **Do not rebuild the post-call pipeline on Step
  Functions/SQS** — the Redis worker queue is retained and adequate at 100–1k calls.
- **Optional augmentation only, for one named gap:** ElastiCache Serverless is already
  multi-AZ with automatic failover, so intra-region AZ loss is well covered. The durability
  properties the Redis Streams queue does *not* give you are (a) **cross-region durability** of
  in-flight (un-acked) jobs, (b) the **narrow window on primary failover** where the most
  recent writes may not yet have replicated to the replica, and (c) a **long retention/replay
  window** (a log-structured broker keeps far more history than a Stream trimmed for memory).
  **If and only if** post-call jobs become business-critical enough to need those guarantees,
  mirror *those specific jobs* to **SQS (with its native DLQ)** as a durable backstop — an
  augmentation beside the Redis queue, **not a replacement of it.** Absent that requirement,
  Redis stands alone.
- **Call-level durability + durable timers + barge-in signals — a SEPARATE concern from the
  background queue.** This is call-orchestration state (silence/filler timers, barge-in signals,
  mid-call recovery), not fire-and-forget background work, and it may warrant its own engine
  independent of the retained Redis worker queue. Adopt an orchestration engine here:
  **If the team can operate Temporal, it is the strongest technical fit** (durable execution
  replaces the hand-rolled checkpoint recovery; durable timers fix the in-process silence/filler
  fragility; signals model barge-in). **If managed-AWS-native is non-negotiable, use Step
  Functions + EventBridge Scheduler** for call lifecycle and durable timers, and **keep the
  existing Redis checkpoint as the mid-turn recovery snapshot**. Either way, **the decider stays
  off the engine's scheduled path** — it is a direct call.

### 5.4 The honest limit (state this to stakeholders)

Distributing the logic makes the system resilient to **worker** failure, deploys of the logic
tier, and load spikes — a real, worth-having win. It does **not** make a call survive a
**gateway** crash, because the gateway owns the two irreplaceable audio sockets: when it dies,
Twilio's media stream closes and the caller hears silence. The Redis checkpoint preserves
*conversational state* for a **reconnect** (Twilio re-`<Stream>`s to a new gateway, which
restores from `call:{call_id}:checkpoint` and resumes — the code path already exists), but
audio continuity depends on Twilio re-establishing the connection. Making gateways *thin,
numerous, and quick to restart* — and keeping checkpoints fresh — is therefore the real
reliability play, not "everything is distributed."

---

## 6. Reliability engineering

### 6.1 Delivery guarantees and idempotency

- **Idempotency key = `(call_id, turn)`** everywhere (already available as `turn_counter[0]`),
  plus `audio_id` (`resp-N`) for TTS and `effect_id` for side-effects. A worker result stamped
  with a turn the gateway has already advanced past is **dropped**.
- **Effectively-once on the hot path:** decide/subagent RPCs are retried on failure; the
  `(call_id, turn)` key + the single mutation entry point `runtime.apply_mutations` make a
  duplicate apply detectable and safe.
- **At-least-once + DLQ on async:** exactly the semantics the retained Redis worker queue
  (`job_processor.py`) already implements (PEL, `XAUTOCLAIM` after 5 min, dead-letter after 3
  deliveries) — keep using this pattern for all new async work rather than introducing a second
  queue system.
- **At-most-once on audio:** a dropped TTS frame is never replayed (a stale frame is worse than
  a gap); recovery is at the *utterance* level (re-speak), which the code already does
  ("Sorry about that. " prefix).

### 6.2 Staleness / TTL on hot-path events (the "5-second-old TTS is useless" problem)

This is already solved in miniature and must be generalized:

- **Generation fencing.** `_eager_gen` / `_speculative_gen` monotonic counters already discard
  stale speculative results. In the distributed world, the gateway attaches the current
  generation/turn to every decide/subagent RPC; a result returning after the gateway has bumped
  the generation (new utterance, barge-in) is **discarded, not spoken**.
- **Hot-path TTLs.** Every hot-path event carries a deadline (e.g. 1.5–2 s). A decision that
  misses its deadline is abandoned and the gateway either re-decides on fresh input or lets the
  filler/silence path take over — **a retried TTS from 5 s ago is never delivered.**
- **Barge-in propagation.** A barge-in cancels the in-flight decide/subagent RPC (gRPC
  cancellation / Temporal signal) *and* bumps the generation, so even a race-completing worker
  result is fenced out. Mirrors today's `_tts_cancel` + `TURN_RESUMED`.

### 6.3 Backpressure (caller outpaces the pipeline)

- The **`utterance_lock` + single-consumer `_utterance_processor`** already serialize turns per
  call; keep this in the gateway. Under load, older un-started turns are **discardable** (per
  `CLAUDE.md`'s backpressure rule) without corrupting state, because all committed state is in
  `DataContext`/checkpoints, not in the queue.
- Worker-tier backpressure: bounded gRPC concurrency + a shed-to-in-gateway-fallback path so a
  saturated worker fleet degrades to local compute rather than queueing unboundedly.

### 6.4 Poison messages and retries

- Async: DLQ after N attempts (already `call_end_jobs:dead`); alarm on DLQ depth.
- Hot path: a subagent that throws is already caught per-step (`return_exceptions=True` in the
  `asyncio.gather`); a repeatedly-failing decide RPC falls back to the planner's existing
  single-step "next primary goal" fallback rather than blocking the call.

### 6.5 Failure + recovery matrix

| Failure | What happens | Recovery |
|---|---|---|
| **Logic worker dies mid-RPC** | Gateway's gRPC call errors/times out | Retry on another worker, or fall back to in-gateway compute. **Call continues.** Both sockets untouched. |
| **Gateway dies** | Twilio media WS + Deepgram TTS WS drop; caller hears silence | Twilio reconnect/`<Stream>` re-dial → routed to a new gateway → `restore_runtime_from_checkpoint` from `call:{call_id}:checkpoint` → `skip_opening=True`, resume. **State preserved; audio interrupted.** |
| **Redis unreachable** | Checkpoints/logs fall back to in-process (existing behavior) | Degraded durability; call still runs. Alarm on fallback. |
| **Decider slow (Cerebras spike)** | Turn latency rises | Filler timer covers it; hot-path TTL abandons a hopeless turn; worker isolation prevents one slow call from blocking others. |
| **Barge-in during distributed decide** | Gateway cancels RPC + bumps generation | In-flight worker result fenced/discarded; `Clear` flushes TTS. |
| **Post-call job fails** | Not acked on the Redis worker queue | PEL reclaim / `XAUTOCLAIM` → DLQ after 3 (existing behavior, retained). |

This ties directly to the existing recovery design
(`project_recovery_design_wip.md`, `project_statelessness_proposal.md`): the checkpoint +
statelessness work is *exactly* what makes the gateway-death path recoverable and workers
relocatable.

---

## 7. Migration path (lowest-risk-first, shippable in stages)

Each step is independently valuable and independently revertible. Nothing here is a big-bang
rewrite; the current single-process path keeps working throughout.

**Step 0 — Close the deployment gaps (days).** Wire `REDIS_ENABLED`/`S3_ENABLED` in
`user_data.sh.tpl`; add a **systemd unit for `worker.py`** (the durable post-call pipeline
exists but is unlaunched). This alone activates existing durability. No code change.

**Step 1 — Harden async events on what's already there (low risk).** Confirm side-effects,
call-log, checkpoint, and post-call all run through Redis Streams with monitoring on DLQ depth
and reclaim rate. Optionally move `SideEffectRequested` fully onto the stream (it is already
fire-and-forget) as the first "emit event → durable consumer" proof beyond post-call.

**Step 2 — Introduce the gateway/logic split *in-process first* (low risk).** Refactor
`handle_call` so the socket-owning half (transport, `utterance_lock`, `turn_counter`,
`_send_queue`, silence/filler/barge-in) is cleanly separated from the compute half
(`planner.plan`, `_invoke_and_follow`) behind an interface — **without** yet moving compute off
the box. This makes the seam explicit and testable. The statelessness-by-contract work already
guarantees the compute half needs only `DataContext` + `log_slice`.

**Step 3 — Decider/subagent over gRPC behind a flag (medium risk).** Stand up
`DecideService` / `SubagentService` (reuse the pattern of the `:8002` classifier host). Flip a
per-workflow flag to route the compute half to the worker pool; keep in-gateway fallback. Prove
the 20–100 ms budget with p99 measurements on real traffic (the metrics pipeline already emits
per-stage latencies via `pipeline_metrics`).

**Step 4 — Session affinity + gateway fleet (medium/high risk — net-new infra).** Add the ASG,
the NLB / per-gateway-hostname routing, and the `call_id → gateway` selection at
`twiml_webhook`. Now calls fan across gateways; a worker or gateway loss no longer takes down
the fleet. Recovery-on-reconnect (already coded) becomes genuinely useful.

**Step 5 — Durable timers + call durability (high value).** Replace the in-process silence and
filler timers with **durable timers** — Temporal `CallWorkflow` if the team will operate it, or
**EventBridge Scheduler + Step Functions** if staying managed-AWS-native. Barge-in becomes a
signal/event. At this point the hand-rolled checkpoint recovery can be *retired* (Temporal) or
kept as the mid-turn snapshot (Step Functions).

**Step 6 — (Conditional) SQS backstop for business-critical post-call jobs.** The post-call
pipeline **stays on the retained Redis worker queue** — do not reimplement it on Step
Functions/SQS. *Only if* a cross-region (or long-retention/replay) durability guarantee for
un-acked jobs becomes a hard requirement (§5.3), mirror those specific jobs to an SQS queue
(native DLQ) as a durable backstop *beside* the Redis queue. Otherwise skip this step entirely.

**Step 7 — MSK only if/when needed (defer).** Introduce MSK/MSK Serverless as the replayable
cross-service backbone **only** when analytics/multi-consumer/event-sourcing demand it — not
before. It is an evolution, not a prerequisite, and it does not replace the Redis worker queue
for background work.

**What already enables all of this:** per-turn Redis checkpoints
(`serialize_checkpoint` + `checkpoint_store`), the working recovery path in `handle_call`,
subagent **statelessness-by-contract** (`node_outputs` + `NodeOutputSet`), the existing durable
Redis-Stream consumer with PEL/reclaim/DLQ (`job_processor.py`), and the existing off-box
decision service (`CLASSIFIER_BACKEND=service`).

---

## 8. Open questions / risks

1. **Temporal vs. Step Functions — who operates it?** Temporal is the stronger technical fit
   (durable timers + signals + execution replaces bespoke recovery) but is **not AWS-managed**
   (self-host or Temporal Cloud). Step Functions is native but a lower ceiling for fine-grained
   mid-call state. This is the highest-leverage decision and depends on the team's appetite to
   run Temporal. **Decide first.**
2. **Gateway-death is unavoidable-drop today.** Are we satisfied with "state survives, audio
   reconnects," or is there a requirement for *seamless* audio failover? True audio HA would
   require dual-homing the media stream or a media-server tier (e.g. an RTP/SIP proxy) in front
   of the gateways — a much larger scope. Recommend accepting reconnect-recovery for v1.
3. **Session-affinity mechanism.** Per-gateway hostnames chosen at TwiML time (Twilio strips WS
   query params, so affinity must live in the *host*) vs. an NLB with connection stickiness.
   Each has different failover and scaling behavior; needs a spike.
4. **Move off the default VPC.** All net-new tiers (worker fleet, gateways, any optional
   MSK/SQS) argue for a proper VPC with private subnets — the current default-VPC, single-EIP,
   `0.0.0.0/0`-SSH setup is not a fit for a multi-tier fleet.
5. **Call-metrics-to-disk is the only per-gateway-local write (minor).** Production already
   stores everything in **Postgres** — call records, gathered parameters, analytics — so the
   shared store is *not* a blocker to a multi-gateway fleet. The one remaining nuance is the
   subset of **call metrics written to local disk** (the `storage/metrics` CSV/tags sink): those
   writes are per-gateway-local, so with a fleet each gateway would hold a partial slice. Before
   or when scaling out gateways, ship those metrics to a shared sink (or centralize/aggregate
   them) so no metric data is stranded on a terminated instance. Low severity — it affects
   metrics completeness, not call correctness or recovery.
6. **gRPC decider p99 under real load.** The 20–100 ms budget is for the *hop*, but Cerebras
   itself is 380–530 ms and was observed unstable in prior probing (memory:
   `project_groq_gptoss20b_findings`). Confirm the worker-tier LLM latency distribution before
   committing to strict per-turn SLAs.
7. **Cost at 1000 calls.** MSK Serverless, a gateway ASG, a GPU/CPU worker fleet, and
   Temporal/Step Functions each carry ongoing cost; model it against the current
   single-`t3.small` + one spot GPU footprint before committing to Option A.
```
