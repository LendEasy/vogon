# Voice Gateway — Roadmap (All Phases)

**Status:** Phase 0 approved and specified ([voice-gateway-phase0.md](voice-gateway-phase0.md)).
Phases 1–2 are draft-level — written down so they constrain Phase 0 correctly, iterated on
after Phase 0 lands. Expect this document to change; the Phase 0 doc should not.
**Scope:** the voice pillar of `data-plane/` and its eventual extraction as a service.

---

## 1. Why a voice gateway

The data plane contains two pillars with different natures, currently in one process:

- **The voice pillar** — Twilio/widget socket handling, STT (Deepgram Flux), TTS (Aura,
  sentence-paced), AEC (`app/transport/aec.py`), barge-in (energy gate + semantic
  absorb/cut), ambience, delivery tracking, recording. Works in 20 ms frames; per-call CPU
  cost is continuous; iterates on the barge/ambience backlog cadence.
- **The reasoning pillar** — planner, workflow engine, agents, extraction. Works in turns;
  cost is bursty LLM-wait; iterates on the agent/eval cadence.

Nothing crosses between them per-frame — the seam is crossed per-turn. That is what makes a
service boundary feasible. The survey that established this (transport inventory, coupling
points, the six polled seam members) is recorded in the Phase 0 doc §7 and in the git
history of this design.

Target architecture, all phases:

```
caller ──── Twilio / widget WS ────┐
                                   ▼
                        ┌─────────────────────┐
                        │    VOICE GATEWAY    │   stateful edge:
                        │  audio, barge, AEC, │   what physically happened,
                        │  clocks, delivery   │   and when
                        └─────────┬───────────┘
                                  │ Turn API (gateway initiates)
                                  ▼
                        ┌─────────────────────┐
                        │    TURN SERVICE     │   stateless:
                        │  planner, agents,   │   what things mean
                        │  workflow engine    │
                        └────┬───────────┬────┘
                             ▼           ▼
                     Redis session    archive
                     store (live)     (post-call)
```

The division of knowledge is the invariant across all phases: **the gateway knows what
physically happened and when; the turn service knows what things mean; the session store
is the only live-call state; the archive is write-once.**

## 2. Phase overview

| Phase | What changes | What it proves / buys |
|---|---|---|
| **0** | In-process restructure: Turn API as a Python interface, Redis authoritative session state, gateway-owned clocks, `vogon/gateway/` + `app/turns/` packages | The contract is complete (sims run against it); state is fully serializable; recovery = normal operation |
| **1** | Process split, same host: Turn API over the network, retry machinery, independent lifecycles | Backend restarts/deploys don't drop calls; the protocol survives a real wire |
| **2** | Independent deploy and scale: separate hosts/replicas, per-persona config, edge placement options | Scale on the right axes; deploy cadences decouple |

Each phase is a strict superset check on the previous one's contract: if a phase needs to
*change* the Turn API's meaning (not extend it), that is a Phase 0 design bug to fix, not
route around.

## 3. Phase 0 — in-process contract (approved)

Fully specified in [voice-gateway-phase0.md](voice-gateway-phase0.md). Summary of what the
later phases inherit from it:

- **Turn API**: `start_call` / `process_events` / `end_call`, gateway-initiated only.
  Every behavior the caller can hear has a gateway-side trigger (greeting → `start_call`;
  probe → silence threshold event; farewell → duration threshold or backend
  `action: end_call`; fillers → gateway policy).
- **Session store**: `call:{id}:state` (conditional writes on `state_version`),
  `call:{id}:log`, staging keys. Backend holds nothing between requests.
- **Spool-and-acknowledge**: the gateway holds physical facts until a request containing
  them is answered. This is what makes network retries in Phase 1 safe.
- **Accepted risk**: Redis down = call fails (decision 2026-07-21, revisit §7).

## 4. Phase 1 — process split, same host (draft)

Goal: the gateway and the turn service run as separate processes on the current host; a
backend restart mid-call does not drop the call (the gateway holds the caller; the next
request goes to the restarted process).

Work items, in rough order:

1. **Turn API over the network.** Protocol choice open (§7). The types are the Phase 0
   dataclasses serialized as JSON; no semantic changes permitted.
2. **Retry + response cache.** Activate `call:{id}:response:{request_id}`: at commit the
   backend stores the response alongside the state; a retried `request_id` for a committed
   turn returns the stored response without reprocessing. This closes the
   duplicate-commit problem that in-process calls never had.
3. **Failure-mode behavior.** Defined per direction:
   - Backend unreachable: gateway retries with backoff; filler policy covers short gaps;
     a configured "trouble" farewell + `end_call(reason=error)` bounds long outages.
   - Gateway dies: the call is gone (it holds the socket). Session state expires by TTL;
     a Twilio reconnect hits `start_call(resume=true)`.
4. **Process packaging.** Gateway and turn service as separate uvicorn services; a
   docker-compose service each plus Redis. The classifier-service pattern
   (`classifier-service/` + `terraform/classifier.tf`: network-only auth inside the
   security group, systemd unit, env-file config) is the deployment template — the
   gateway is CPU-bound, so no GPU machinery.
5. **Artifact endpoints move.** Recording/audio-timeline HTTP endpoints move to the
   gateway (it owns the WAV/frames artifacts).
6. **OpenAI Realtime path decision.** It lives in the gateway process (it is an audio
   bridge); decide whether it adopts `start_call`/`end_call` for config and analytics or
   keeps its bespoke seam.

Open questions for Phase 1 (decide at Phase 1 kickoff, not before):

- Transport: persistent WebSocket per call vs HTTP request per turn. HTTP is simpler and
  matches the RPC semantics exactly; a WS saves per-turn connection overhead. Same-host
  latency makes this a robustness choice, not a performance one.
- Whether `process_events` responses should be streamable (would allow backend-paced
  multi-part responses; Phase 0 deliberately kept one-shot responses + gateway fillers).
- Where metrics emission lands once the processes split (both sides have counters; the
  sink is currently data-plane-local).

## 5. Phase 2 — independent deploy and scale (draft)

Only sketched; shaped by what Phase 1 teaches.

- **Backend replicas.** The conditional-write commit already tolerates multiple turn
  service replicas; scaling out is a load-balancer change. Per-call request ordering is
  preserved by the gateway (one outstanding request per call).
- **Gateway scale-out.** Gateways scale on concurrent-call count. Calls pin to a gateway
  instance for their lifetime (the socket lives there); no cross-gateway state.
- **Per-persona voice config from the control plane.** The `TransportConfig` slot exists
  from Phase 0; Phase 2 wires per-assistant barge/threshold/filler values through the
  config export (TODO #11 in the voice backlog).
- **Edge placement.** Gateways near Twilio regions, turn service + Redis central. Only
  worth doing with real geographic latency evidence.
- **DSP language freedom.** The pure modules (AEC wrapper, barge gate, mixer,
  sentence split) are candidates for a non-Python implementation if the 20 ms frame loops
  ever become the per-host ceiling. Nothing in the contract assumes Python.

## 6. Relationship to prior designs

- **`../../docs/call-state-recovery.md`** — absorbed. The checkpoint it designed is
  promoted to the authoritative session state in Phase 0; recovery stops being a special
  path (`start_call(resume=true)` reads the same store every turn reads).
- **`distributed-call-architecture.md`** (this directory) — narrowed. Its core-tension
  analysis (the two realtime connections cannot be brokered without destroying call
  quality) is honored: both stay in the stateful gateway. Its broader proposal (durable
  event bus, consumer workers) is **not** the chosen direction; this roadmap chooses a
  stateful edge + stateless request/response turn service + Redis conditional writes
  instead. Where the two documents conflict, this one governs.
- **Statelessness proposal / recovery WIP (project memory)** — this roadmap is their
  completion: the agent statelessness contract (`node_outputs` as sole cross-turn state)
  is the property that makes the turn service possible.

## 7. Deferred decision register

| Decision | State | Revisit when |
|---|---|---|
| Redis down = call failure | Accepted (2026-07-21), no mitigation built | After Phase 1, with real outage/SLO data |
| Call log stays live per-event (no atomic append-at-commit) | Decided (2026-07-22): live streaming is the contract; speculative/interrupted turns log too; log/state atomicity is decoupled from the gateway program | Only if Phase 1 retries demonstrably double-log — then as its own change, its own decision |
| Side-effect executor (SMS, links) | Recorded in `pending_side_effects`; execution mechanism unchanged | Phase 1 (a stateless backend cannot own fire-and-forget tasks across a deploy) |
| Turn API network transport (HTTP vs WS) | Open | Phase 1 kickoff |
| Response streaming | Rejected for Phase 0 (gateway fillers instead) | Phase 1, only if filler policy proves insufficient |
| OpenAI Realtime integration with Turn API | Untouched in Phase 0 | Phase 1 item 6 |
| Per-persona thresholds via control plane | Slot exists in `TransportConfig`; values from settings | Phase 2 |
| Session-state schema migration tooling | Rule set in Phase 0 (migrate-on-read, never discard) | First deploy that changes the schema |
