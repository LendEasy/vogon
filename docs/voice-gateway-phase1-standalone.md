# Voice Gateway — Phase 1: Standalone Service Plan

**Status:** M1 COMPLETE — deployed to production and user-verified
2026-08-25 (two services under systemd, nginx /gw prefix on the shared
voice domain, static service secrets, cutover via GATEWAY_URL). Phase 0
was completed 2026-08-02. This document specified the process split: the
gateway and the application (turn service) as two services with an HTTP
seam, the gateway reusable by any voice application. Remaining threads
live in §6 M2/M3 and the follow-through notes inside the checklist.

**Supersedes** the draft Phase 1 section of `voice-gateway-roadmap.md` where
they differ. The Phase 0 contract (`vogon_contract/types.py`) keeps its
meaning — this plan extends it, per the roadmap's superset rule.

---

## 1. Definitions

- **Voice gateway** — the stateful edge service: sockets (Twilio media
  streams, browser widget), STT/TTS, AEC, barge-in, ambience, clocks,
  recording. Knows what physically happened and when. Never knows what
  anything means.
- **Application (turn service)** — the reasoning half: planner, agents,
  workflow engine. Knows what things mean. Today this is the rest of
  `data-plane/`.
- **Turn API** — the three calls the gateway makes to the application:
  `start_call`, `process_events`, `end_call`. Gateway always initiates.
- **App payload** — an opaque JSON object supplied when a call is created,
  stored by the gateway, and passed verbatim to the application on
  `start_call`. The gateway never reads it. This replaces the gateway
  knowing `owner_id` / `workflow_id`.
- **Tenant** — the gateway's scoping namespace. An opaque string to the
  gateway (it never interprets it), but every call, artifact, metric row,
  quota, and config profile is keyed under exactly one tenant. For
  codelexin, tenant = `owner_id`; the optional **route** sub-scope =
  `workflow_id` (matching today's metrics partitioning).
- **Session store** — the gateway's persistent record of live and recent
  calls (transport state only, never meaning). An injected interface with
  a versioned schema; Redis-backed in production, in-memory for tests.
  Distinct from the application's `CallRecord`, which remains the business
  record — two records, one call, joined by `call_id`.
- **TenantProfile** — the per-tenant configuration document (STT/TTS/
  barge/ambience/recording defaults) in a versioned schema, so a control
  plane can serve it later. Distinct from `GatewayConfig` (deployment-level:
  credentials, ports) and from per-call `CallConfig`.
- **CallConfig** — the per-call configuration the application returns from
  `start_call`. This is Phase 0's `TransportConfig` **renamed**: with three
  config layers in play, each name says whose config it is and where it
  comes from (GatewayConfig = this deployment, TenantProfile = this tenant,
  CallConfig = this call).

## 2. The revised interface — opaque payload over POST

### 2.1 Call creation (client/application → gateway)

The gateway gets its own small HTTP API, **tenant-scoped in the path**. A
call comes into existence when the application (or any authorized client)
POSTs:

```
POST {gateway}/v1/t/{tenant}/calls
{
  "mode": "twilio" | "widget" | "text",
  "route": "collections_demo_v3",  # optional sub-scope; opaque label —
                                   #  partitions metrics/artifacts, defaults
                                   #  the CallConfig.workflow_id slot
  "app": {
    "base_url": "http://app-host:8001/turn/v1",   # where the Turn API lives
    "auth_token": "...",                           # bearer for turn calls
    "status_webhook": "https://..."                # optional: gateway POSTs
                                                   #  call-state transitions
                                                   #  (best-effort)
  },
  "app_payload": { ... },        # opaque; passed through on start_call
  "twilio": {                    # mode=twilio only
    "to": "+1...", "from": "+1..."
  }
}
→ 200 {
  "call_id": "...",
  "session_token": "...",
  "ws_url": "wss://gateway-host/ws/call?token=..."   # widget/text modes
}
```

The gateway API key presented here is **bound to the tenant** — a key for
tenant A cannot create or read calls under tenant B. For codelexin,
tenant = `owner_id`, `route` = `workflow_id`, and `app_payload` carries
`{owner_id, workflow_id}` (or whatever the application chooses — the
gateway does not read it). The codelexin `POST /api/v1/calls/initiate` and
`POST /api/v1/twilio/initiate` routes keep their external contract: they
authenticate the customer, fetch + prime config from the control plane
(pre-dial, unchanged rule — now enforced application-side), write
`CallRecord`, then call the gateway's `/v1/t/{owner_id}/calls` and relay
`ws_url`.

Today's DB lookups at the WS endpoints (`main.py:130,252` — `CallRecord`
by token / by CallSid) are replaced by the gateway's **session store**
(§2.4): the record is minted at call creation, the provider call id
(CallSid) is bound to it at dial time, and the TwiML webhook + media WS
resolve against it. **The standalone gateway has no application database
and no ORM** — its one persistence dependency is the session store,
injected behind an interface with a supplied, versioned schema.

Twilio-physical routes move to the gateway: the TwiML webhook, the status
webhook, the media-stream WS endpoints, and the REST dial. `PUBLIC_BASE_URL`
becomes gateway config. The moved status webhook updates the gateway's
session record and relays the transition to `app.status_webhook` when one
was registered — that is how the demo page's `CallRecord.state` poller
keeps working without the gateway touching the app's DB.

### 2.2 Turn API over POST (gateway → application)

The Phase 0 dataclasses serialize as JSON (they were designed for this —
every event already carries a `kind` wire tag). Changes to the shapes:

```
POST {app.base_url}/start_call
{ "call_id", "mode", "resume", "payload": <app_payload, verbatim> }
→ StartCallResponse   (call_config, speak[], client_events[], state_version)

POST {app.base_url}/process_events
{ "call_id", "request_id", "events": [ {kind, ...}, ... ] }
→ ProcessEventsResponse   (speak[], action, end_reason, client_events[])

POST {app.base_url}/end_call
{ "call_id", "reason", "transport_analytics": [...], "provider_call_id" }
→ {}
```

Contract deltas from Phase 0:
- `StartCallRequest`: **drop `owner_id` and `workflow_id`, add
  `payload: dict`.** They were the application's identities; they move
  inside the payload. `workflow_id` stays on the per-call config — it is
  already documented as an opaque telemetry label the application hands
  *back*.
- `TransportConfig` is **renamed `CallConfig`** (its `StartCallResponse`
  field `transport_config` → `call_config`) so the three config layers
  read as what they are. The rename lands with M1 item 1 and needs the
  reader sweep a rename always needs here: the transports' constructors,
  the AST constructor fence and the voice-pillar import tests name it, and
  `gateway_dependency_kit` builds it — grep before, update in the same
  commit.
- `CallConfig` gains two blocks the gateway currently hardcodes or
  reads from shared settings (see §4): `recording` (enabled, channels,
  artifact retention) and `barge_policy` (safety phrase list, judge
  enablement). Both are application decisions — different voice apps have
  different compliance needs.
- Everything else in `contract.py` crosses unchanged.

Auth (REVISED 2026-08-25, user decision — static service secrets for all
service-to-service edges): customer identity lives at the APP's front door
only, validated once against the control plane; it never travels inward
(the earlier tenant-key-at-the-gateway design double-validated the same
key against the same authority and is REMOVED). The app↔gateway pair
shares one static `GATEWAY_SERVICE_KEY` (create/read calls AND the status
relay back); the gateway↔turn pair shares `TURN_SERVICE_API_KEY` (the
per-call `app.auth_token` slot stays in the contract for the future
bring-your-own-turn-service case but codelexin passes it empty — nothing
secret persists on the session record). The end user's only gateway
credential is the per-call ws token; Twilio authenticates by
token/CallSid (X-Twilio-Signature validation = recorded hardening).
Tenant-scoped gateway keys return only if a direct-customer gateway
surface ever exists — minted by the control plane at that point.

### 2.3 What dies at the seam

`app/turns/api.py`'s real signatures are not contract-only today — this is
the enumerated cut list:

| In-process residue | Standalone replacement |
|---|---|
| `start_call` returns `TurnContext`; `on_context` callback | Application HTTP server holds its own `call_id → TurnContext` registry (in-memory, call-affinity — see §6 M1 scope). Nothing crosses. |
| `start_call(config=, db_session=, redis=, instance_id=)` | Application-side resources, acquired by the application's own HTTP handlers. |
| `DriverPort.pending_facts` (mutated by both halves; `executor.py:250` clears it by slice-assignment) | Spool custody moves fully gateway-side: the gateway batches facts into `ProcessEventsRequest.events` and clears its spool when the response arrives (spool-and-acknowledge, exactly as Phase 0 §4 designed). The turn side reads `request.events` only. |
| `DriverPort.recorder` + `lifecycle.py:517` firing `save_and_upload` | Gateway fires recording finalization at socket close (where `frames.csv` and the audio event log already flush). Turn side never holds the handle. |
| `DriverPort.send_call_ended` (`lifecycle.py:621`) | The gateway emits `server.call_ended` itself at teardown — it owns the widget socket and knows the reason. |
| `DriverPort.begin_hold`/`end_hold` (barged-filler discard between compute and compose) | Gateway-local holds (hold = request in flight) + discard protocol — open decision D4, §8. |
| `api.close_call_log` driver-invoked (`handler.py:559`) | Application closes its own log inside its `end_call` handler, after awaiting call-end tools. Lifecycle control stops crossing the seam. |

`DriverPort` is deleted when this table is done.

### 2.4 Session store — the gateway's one persistence dependency, modeled

The gateway depends on a persistent store, and that dependency is
**explicit, injected, and schema'd** — never reached through an ORM or
another service's tables. It replaces the three DB lookups the gateway
half does today (token → call at `/ws/call`, CallSid → call at
`/ws/twilio`, mode at `/twiml`) and makes them survive a gateway restart
during ringing and work from any gateway replica (Twilio webhooks hit
whatever instance the load balancer picks; only the media socket pins).

```
class GatewaySessionStore(Protocol):            # injected at boot
    async def create(self, rec: CallSessionRecord) -> None
    async def get(self, tenant: str, call_id: str) -> CallSessionRecord | None
    async def get_by_token(self, token: str) -> CallSessionRecord | None
    async def get_by_provider_id(self, provider_call_id: str) -> CallSessionRecord | None
    async def bind_provider_id(self, call_id: str, provider_call_id: str) -> None
    async def transition(self, call_id: str, state: str, at_utc: float) -> None
    async def claim_socket(self, call_id: str, instance_id: str) -> bool   # CAS: one socket owner
```

`CallSessionRecord` schema v1 (versioned; migrate-on-read, never discard —
the Phase 0 session-schema rule applies here too):

```
CallSessionRecord:
  schema_version: 1
  tenant: str                    # scoping namespace (§2.6)
  route: str = ""                # opaque sub-scope label
  call_id: str                   # gateway-minted
  session_token: str             # secondary index
  provider_call_id: str = ""     # Twilio CallSid; secondary index, bound at dial
  mode: twilio | widget | text
  state: created | dialing | ringing | connected | ended | failed | cancelled
  app_base_url: str
  app_auth_token: str            # stored value; a *_ref indirection is a later hardening
  app_status_webhook: str = ""
  app_payload: dict              # opaque, echoed on start_call
  call_config: dict | None      # resolved snapshot cached after start_call
  gateway_instance: str = ""     # which replica holds the socket (claim_socket)
  created_at_utc / connected_at_utc / ended_at_utc: float
```

Backends: `RedisSessionStore` (production — hash per call under
`gw:{tenant}:call:{call_id}` plus two index keys for token and provider id,
TTL after `ended`) and `MemorySessionStore` (tests, single-process dev).
The store carries **transport state only**: no transcript, no meaning, no
app data beyond the opaque payload. The application's `CallRecord` remains
the business record; the two never share a table, and the status-webhook
relay (§2.1) is how the app keeps its record in sync.

### 2.5 Configuration — explicitly modeled, control-plane-ready

All gateway configuration today is one flat env-driven `settings` object
shared with the application. The standalone gateway models it as **three
layers with explicit schemas and a defined precedence**, so a control
plane can later serve layer 2 without touching code:

```
Layer 1 — GatewayConfig (env / file; static per deployment; never per-call)
  ports, PUBLIC_BASE_URL, gateway API keys (per tenant), session-store URL,
  provider credentials: DEEPGRAM_API_KEY, TWILIO_* , OPENAI_API_KEY,
  CEREBRAS_API_KEY (D1), S3 credentials, capability policy (D5:
  refuse/warn on missing AEC/VAD), config-source: static | control-plane

Layer 2 — TenantProfile v1 (per TENANT; versioned pydantic schema;
          source = static YAML/env today, control-plane export later —
          fetched + cached BEFORE dial, same pre-connect rule as Phase 0)
  stt:       model, EOT thresholds, eager thresholds, keyterm policy
  tts:       model, default voice, pacing, phone encoding
  barge:     energy-gate tuning, semantic-judge on/off + timeout,
             default safety-phrase list
  ambience:  defaults (enabled, gains, delays)
  recording: default policy (enabled, channels, retention)
  filler:    default FillerPolicy
  metrics:   partitioning root

Layer 3 — CallConfig (per CALL; from the application at start_call;
          already in the contract as TransportConfig, renamed per §2.2)
  persona voice/name, language, name_hint, thresholds, filler, ambience,
  recording (§3.2), barge_policy (§4.1)
```

**Precedence: CallConfig > TenantProfile > built-in defaults**,
resolved once at call start into a frozen snapshot that is stored on the
`CallSessionRecord` — so a webhook served by another replica, a resumed
call, and the audit trail all see the same resolved values, and "which
config was this call under" is answerable after the fact. The 52
audio-domain keys the gateway currently reads from `app.config` all land
in layers 1–2 (credentials → 1, tunables → 2); nothing stays ambient.
This is deliberately the same shape codelexin already uses for the
application (control-plane config export, primed pre-dial), so wiring a
control plane to serve `TenantProfile` later is a fetch, not a redesign.

### 2.6 Multi-tenancy

The tenant string from the path scopes every surface the gateway has;
the gateway never interprets it. Enforcement points:

1. **Auth** — the management surface takes the app↔gateway pair secret
   (service trust; its only caller already authenticated the customer);
   the tenant path segment is trusted caller data. (REVISED 2026-08-25 —
   per-tenant gateway keys removed; see §2.2 auth note.)
2. **Session store** — all keys prefixed `gw:{tenant}:` and `get()`
   requires the tenant; token and provider-id indexes resolve to a record
   that carries its tenant, checked against the caller's scope.
3. **Artifacts** — storage layout `{tenant}/{call_id}/recording.wav`
   (local and S3); artifact routes are `/v1/t/{tenant}/calls/{id}/...`
   and verify record tenant.
4. **Metrics** — CSV/sink partitioned `{tenant}/{route}/...`, which is
   compatible with today's `{workflow_id}` partitioning when codelexin
   passes route = workflow_id.
5. **Config** — `TenantProfile` is selected by tenant (Layer 2).
6. **Quotas (slot in the schema, enforcement deferred)** — max concurrent
   calls per tenant lives in `TenantProfile`; M1 records the count,
   enforcement is M2.

Compatibility: codelexin's identities map with no data migration —
tenant = `owner_id`, route = `workflow_id`. Inbound calling (future)
extends the same model: a phone-number → {tenant, app binding} table in
the gateway's Layer 1/2 config, which is why the `app` block lives per
call-creation rather than per deployment.

### 2.7 The ledger and its views (decision 2026-08-25, extends D4)

The call ledger — `call:{id}:log`, the append-only per-call stream with a
total order (the Lua INCR+XADD from `44c4862`: stream order == redis_seq by
construction, gaps detectable) — is the SHARED record all services read.
**State is a fold of the ledger; every consumer-facing history is a
read-time projection (view) over it.** Nothing coordinates at write time
beyond appending; disagreement between "what was said" and "what was
heard" is not an error state but two views of the same rows.

The system already works this way in its most important read path: the
history handed to the LLM is horizon-rewritten from what the caller
actually HEARD (full-duplex-turn-model §A), not the transcript-as-said.
This section names the pattern and makes it a contract.

**Entry envelope (every entry, both writers):** `redis_seq` (total order),
`t` (call-relative seconds, §3.4 — one timeline), `request_id`, `turn`,
`kind`, `payload`, and the physical join key (`audio_id`) where one
exists. An entry missing envelope fields is a defect: it breaks every
view downstream.

**Writers.** Semantic entries: the turn service only. Physical entries:
the gateway's facts today enter via `ProcessEventsRequest.events` and are
logged turn-side (single writer, order trivially total); post-split the
gateway MAY append purely-physical entries directly (the Lua script was
built for multi-writer order). Rule: no side-channel writes — the
`LLM_CALLED` bypass (T4) is the counterexample: entries that skip
`CallLog` make the archived ledger and the in-memory log different event
sets, and every view built from one lies about the other.

**Views (all derived, all replayable offline — the evaluator is the
existing proof that replay works):**

| View | Built from | Consumer |
|---|---|---|
| HeardHistory | UTTERANCE + SPEAK_EMITTED ⋈ delivery facts by audio_id; unheard/partial speech rewritten out | every classifier/LLM prompt |
| Transcript (as-said) | UTTERANCE + SPEAK_EMITTED verbatim | humans, archive |
| SessionState | fold of MUTATION/commit entries | recovery, audit of the snapshot |
| DeliveryTimeline | playout/barge/drain facts | barge tuning, the audio timeline |
| Analytics | latency/counter rows | metrics, dashboards |
| Compliance | obligations, guardrail trips, recited lines | audit |

**Snapshot discipline.** The session-state snapshot (`call:{id}:state`,
CAS on `state_version`) stays the live-call fast path — a CACHED FOLD,
not a second truth. Each commit's `REQUEST_COMPLETED` entry carries the
`state_version` it produced, so `snapshot(vN) ==
fold(ledger through that entry)` is a checkable invariant, and
reconstruction from the ledger is always possible when the snapshot is
gone or doubted.

**What this requires of M1 (already itemized):** the clock normalization
(item 7 — one call-relative timeline or the ledger's `t` lies across
processes), the T4 bypass fix (item 8 — ledger completeness), and
`state_version` stamped into `REQUEST_COMPLETED` (item 8, one line).

## 3. Logging and call recording — the abstraction

The audit found the two halves' artifacts are already almost cleanly split;
the plan makes the split an interface.

### 3.1 Ownership rule

**Gateway-owned (physical):** stereo call WAV (L=caller, R=agent),
`audio_events.json`, `frames.csv`/`clean.wav` diagnostics, the audio
timeline renderer, transport analytics counters, per-playout delivery
timings, process logs. **Application-owned (meaning):** `CallLog` (every
`EventType` append is already turn-side — the gateway writes zero entries
today, verified), transcripts, `CallAnalytics` DB rows, LLM sidecar, S3
archival worker for those artifacts.

The template is the one seam this repo already solved: `transport_analytics`
rides `EndCallRequest` **as data**. Physical facts flow up as events; the
application logs meaning on its own side.

### 3.2 Recording abstraction (gateway)

```
class ArtifactStore(Protocol):          # gateway-level, configured at boot
    async def put_file(self, key: str, path: Path) -> None: ...

class CallRecorder(Protocol):           # per call
    def set_stream_epoch(self, t: float) -> None
    def add_caller_chunk(self, at: float, mulaw: bytes) -> None
    def add_agent_chunk(self, at: float, mulaw: bytes) -> None
    async def finalize(self) -> list[ArtifactRef]   # WAV + event log + diagnostics
```

- `LocalDirStore` and `S3Store` implement `ArtifactStore`; the gateway's
  own config picks one (today's `recordings_path` / `s3_*` keys land in
  GatewayConfig, §2.5). Artifact keys are tenant-scoped:
  `{tenant}/{call_id}/recording.wav` (§2.6). The `app.services.s3_store`
  import is replaced by a gateway-local copy of the ~60-line uploader.
- Per-call enablement comes from `CallConfig.recording` — the
  application decides whether a call is recorded, the gateway decides how.
- Fixes folded in: the `_recorder._t0` private reach-in
  (`twilio_transport.py:754`) becomes a public `epoch_offset()` accessor;
  the recorder is constructed by the transport factory, not the driver.
- The artifact HTTP routes that read gateway-written files
  (`/recording.wav`, `/audio-timeline`, `/audio-events.json`,
  `/frames.csv` — `calls.py:592-628`) **move to the gateway service**
  (roadmap Phase 1 item 5). The application's `/artifacts` route (S3
  presigned) stays and keeps working for archived calls.

### 3.3 Call log custody (application)

- `run_log_writer` + `append_log_entry` (the Lua INCR+XADD) move out of
  `vogon/websocket/call_persistence.py` into the turns package — the file's
  own docstring anticipates this. The gateway stops linking any CallLog
  code.
- Known defect to fix in passing (T4): `executor.py:116-131` pushes a
  hand-built `LLM_CALLED` line straight onto the writer queue, bypassing
  `CallLog` — the archived JSONL and the in-memory log are different event
  sets, and those lines carry no `order_seq`/`request_id`. Route it through
  `CallLog.append` like everything else.
- Gateway process logging (`call_id` contextvar + filter) survives
  unchanged in both services — it is process-local by construction.
- Gateway metrics labeling (T7): the driver reads `workflow_id` off
  `TurnContext` to partition its CSV metrics. Read it from
  `CallConfig.workflow_id` instead (already in the contract as an
  opaque label); the gateway gets its own `MetricsSink` instance and
  directory.

### 3.4 The clock seam (T1 — correctness, silent if ignored)

Event timestamps (`onset_at`, `at`) are stamped on the gateway's
`time.monotonic()` and consumed turn-side as `occurred_at` and as
transcript offsets against a `call_epoch` taken on the *turn side's*
clock. Two processes' monotonic clocks share no epoch — after the split
this breaks silently (wrong ordering, nonsense transcript offsets), no
error raised.

**Decided 2026-08-25 (user):** ORDER never comes from time — the ledger
orders by ENTRY NUMBER, always (`CallLog.ordered()` keys on `order_seq`;
`occurred_at` is an annotation a view may display by, never a sort key).
The GATEWAY's clock is authoritative for physical time; it passes the
timestamps; the turn service OFFSETS them onto its own clock and treats
the passed value as the authoritative time.

Fix at the edge: on the wire, `at`/`onset_at`/`carry_forward.at` travel
as **call-relative seconds** — the sender shifts by −its_epoch
(HttpTurnClient, epoch minted just before start_call), the receiver
rebases by +its_epoch (`_TurnCall.epoch`, minted on receiving
start_call) via `contract_codec.shift_event_times`. A clock value never
leaves the process that minted it; every delta is exact; same-domain
comparisons (exchange anchoring) stay same-domain untouched; the mixed
sites (carry-forward age, transcript offsets, ledger t) heal without
being edited. The application treats event
times as opaque gateway-authoritative values for offsets, and
stamps wall-clock (`started_at_utc`) itself only for calendar display.

## 4. Genericity gaps found in gateway code (must fix to be reusable)

1. **`barge_semantics.py:53-61` hardcodes debt-collection vocabulary**
   (`"attorney"`, `"stop calling"`, `"take me off"`, …) as
   `SAFETY_PHRASES` — a domain noun set inside the gateway, violating the
   platform/domain rule. Move the list into `CallConfig.barge_policy`;
   the codelexin application supplies today's exact list, so behavior is
   unchanged.
2. **Semantic barge-in calls Cerebras from the transport layer**
   (`barge_semantics.py:28` → `app.llm.structured` →
   `app/pipeline/llm.py`), on by default and time-capped. The gateway is
   not LLM-free. Decision D1 (§8); default proposal: the gateway keeps a
   small, self-contained "barge judge" client (provider + key in gateway
   config, prompt neutral, phrase lists from `CallConfig`) so the
   verdict stays on the latency path it was tuned for.
3. **`openai_realtime_transport.py:239` names an agent tool
   (`process_turn`) and injects borrower context** — but the whole
   realtime handler bypasses the Turn API and is a second producer of
   every artifact (1050 lines, imports agents/framework directly).
   Decision D2 (§8); default proposal: park it — the realtime path stays
   in the monolith, the standalone gateway ships without it.
4. **`app.config.settings`** — the gateway reads 52 keys, all
   audio-domain (`deepgram_*`, `stt_*`, `tts_*`, `barge_in_*`,
   `ambience_*`, `semantic_barge_*`, `openai_*`, `recordings_path`,
   `s3_*`). They form a clean standalone settings class with no
   application config mixed in. Create `gateway settings` (own env file);
   the gateway stops importing `app.config`.
5. `app.textutils.contains` (stdlib-only helper) — copy or extract to a
   shared util; trivial.

## 5. Non-Python and third-party dependencies (the documentation)

To ship as `DEPENDENCIES.md` beside the gateway's own `requirements.txt`.
No Docker images now; this is the record a future image build follows.

### 5.1 System packages (native builds)

| Dependency | Needed for | System packages | Failure mode today |
|---|---|---|---|
| **SpeexDSP AEC** (`speexdsp @ git+https://github.com/xiongyihui/speexdsp-python.git`, SWIG/C++ build) | Echo cancellation (`vogon/gateway/transport/aec.py`) | `git g++ swig libspeex-dev libspeexdsp-dev` (list confirmed in `docs/aec-installation.md`) | **Silently degrades to no AEC** (`create_aec()` → `None`) — callers get echo with zero errors |
| **webrtcvad** (C extension, unmaintained upstream; `webrtcvad-wheels` is the maintained fork) | Speech-gated barge confirm; audio-timeline VAD lane | build tools if no wheel for target Python | Silently degrades to energy-only barge gate |
| **stdlib `audioop`** | ~20 call sites: mulaw↔linear16, resample, RMS — the entire transcode path | none — but **removed from Python 3.13** (PEP 594) | Hard break on Python ≥3.13. Pin ≤3.12 or add `audioop-lts` |
| numpy | GCC-PHAT delay estimation in AEC alignment (not per-frame hot path) | wheel | — |

**Notably absent:** no ffmpeg, no sox, no subprocess audio tools anywhere in
the gateway half. All transcoding is in-process `audioop`.

**Plan item:** replace both silent degradations with a startup capability
check — log a single WARN/refuse-to-start (configurable) when AEC or VAD
cannot load. A containerized gateway running with AEC quietly disabled is
the worst version of this failure.

### 5.2 External services

| Service | Use | Protocol | Env |
|---|---|---|---|
| **Deepgram STT** | Flux v2 listen, streaming; mulaw 8 k passthrough on phone | WS via `deepgram-sdk==6.0.1` | `DEEPGRAM_API_KEY`, `DEEPGRAM_STT_MODEL`, EOT tuning vars |
| **Deepgram TTS** | aura-2, one persistent WS per call, mulaw/8 k phone override | WS, same SDK | `DEEPGRAM_TTS_MODEL` |
| **Twilio media streams** | inbound/outbound audio: base64 **mulaw 8 kHz, 20 ms frames**, raw JSON — no SDK on the media path | WS (gateway serves) | `PUBLIC_BASE_URL` (webhooks + stream URL) |
| **Twilio REST** | dial, warm transfer, recording flags | `twilio>=9.0.0` | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` |
| **Cerebras** | semantic barge judge only (D1) | `cerebras-cloud-sdk` | `CEREBRAS_API_KEY` — only if judge stays in-process |
| **OpenAI Realtime** | alternate S2S transport — parked (D2) | raw `websockets` | `OPENAI_API_KEY` |
| **AWS S3** | recording/artifact upload (optional) | `boto3` | `S3_ENABLED`, `S3_BUCKET`, `S3_REGION`, cred chain |
| **Redis** | gateway is a best-effort *writer* only (log stream hand-off goes away with log custody; job enqueue TBD); the strongly-consistent session state is application-side | `redis[hiredis]` | `REDIS_URL` — optional for the gateway |

### 5.3 Gateway `requirements.txt` (proposed)

```
fastapi  uvicorn[standard]  websockets  pydantic  pydantic-settings
python-dotenv  python-multipart  httpx            # httpx: the Turn API client
deepgram-sdk==6.0.1  twilio>=9.0.0
speexdsp @ git+https://github.com/xiongyihui/speexdsp-python.git
webrtcvad>=2.0.10  numpy  aiofiles  boto3>=1.34  redis[hiredis]>=5.0
cerebras-cloud-sdk   # only under decision D1's default
audioop-lts; python_version >= "3.13"
```

Dropped relative to the monolith: `torch`, `transformers` (the big image
win), `jellyfish`, `sqlalchemy`, `psycopg2-binary` (the gateway has no DB).

### 5.4 Static assets

None committed. Ambience synthesizes room-tone/typing when
`assets/*.wav` are absent (`ambience/loader.py`); the assets README
documents authoring specs. Filler audio is text through normal TTS, not
pre-rendered. (Fix in passing: `loader.py:3` docstring cites a stale
asset path.)

## 6. Work plan

### M1 — bare minimum to run the gateway standalone

Two uvicorn processes from this repo, separate entrypoints and env; **no
file relocation into a new top-level directory yet** (the earlier ruling
stands: moving files is cosmetic, the coupling is in the seams — relocation
happens in M3 packaging once the import fence proves the boundary).

Ordered:

```
[x] 1. Contract serialization: dataclass <-> JSON for every contract.py type
       (kind-tagged event union); StartCallRequest drops owner_id/workflow_id,
       gains payload; TransportConfig renamed CallConfig (reader sweep,
       see §2.2) and gains recording + barge_policy blocks.
       DONE 2026-08-24: vogon_contract/codec.py (to_wire/from_wire,
       type-hint-driven decode, unknown fields ignored, missing required
       raises); 20-file reader sweep, zero stale refs; identities ride
       payload end-to-end (handler posts, lifecycle reads); 281 focused
       tests green incl. round-trips for every contract type.
[x] 2. Turn service HTTP app (application side): POST /turn/v1/{start_call,
       process_events,end_call}; in-memory call_id -> TurnContext registry
       (single worker, call-affinity — Redis-served context is M2);
       per-call request_id response cache for safe retries; close_call_log
       runs inside end_call.
       DONE 2026-08-24: app/turns/http_service.py (router mounted in
       app/main.py) — per-call lock serializes requests, start_call retry
       returns the stored opening, end_call idempotent + driver-order
       teardown (tools -> close log -> drop), server-side DriverPort
       stand-ins (null holds/recorder — burn-down is item 4), D3 config
       resolver (payload.session_token -> primed cache, miss = 409),
       optional bearer auth (settings.turn_service_api_key); 8 tests in
       tests/test_turn_http_service.py, ledger gains contract_codec.
[x] 3. Gateway TurnClient (httpx, timeouts, one outstanding request per
       call) replacing the api.* function calls in the driver.
       DONE 2026-08-24: vogon/websocket/turn_client.py — TurnClient surface
       (start/process/end/flush_facts/close + started); InProcessTurnClient
       quarantines the Phase 0 residue (ctx/driver/config/db/redis bind,
       on_context, teardown ordering) and calls turns_api via module attrs
       so existing doubles keep intercepting; HttpTurnClient = POSTs with
       per-call lock + bounded retry (safe via server request_id cache /
       stored opening / idempotent end); selected by settings
       .turn_service_url (empty = in-process, live behavior unchanged).
       IN-PROCESS CLIENT DELETED 2026-08-29 (M3 follow-through, see the M3
       section): HttpTurnClient is the only client, an unresolvable address
       raises TurnServiceNotConfigured, and the gateway refuses a blank
       app.base_url at call creation. The driver-side app.turns ledger entry
       went with it.
       Driver is contract-only at the seam (handler imports of api/executor/
       TurnContext deleted; ledger shrunk + new pin test); metric label now
       reads CallConfig.workflow_id (T7 driver half done); payload gains
       session_token for D3. HTTP-mode gaps owned by item 4: no filler
       window, recording finalize, terminal widget frame. 306 focused
       tests green; 6 new in tests/test_turn_client.py.
[x] 4. DriverPort burn-down (§2.3 table): spool custody to the gateway,
       recorder finalize at socket close, gateway-emitted call_ended,
       gateway-local holds with the D4-decided discard shape. Delete
       DriverPort.
       DONE 2026-08-25: app/turns/driver_port.py DELETED. Facts are
       classified once in api (_FACT_EVENT_TYPES) and processed straight
       from request.events — which also FIXED the pinned DeliveryOutcome
       omission (strict xfail -> passing test); the spool's final flush
       rides EndCallRequest.events. Recorder fires at socket close;
       server.call_ended is the driver's frame; holds are driver-local
       around the outstanding request with the D4 LEDGER rule (barged
       filler -> suppress playout + truncated DeliveryOutcome fact,
       nothing rolled back — pinned by
       test_barged_filler_suppresses_playout_and_reports_the_outcome).
       executor's ~15 dead driver params swept; evals gateway_sim
       repaired (had been broken since item 1); http_service finalizes a
       part-bound ctx on failed start (item-2 gap). Ledgers shrunk;
       handler pin = {app.models.call_record}. 314 focused green.
[x] 5. Session store (§2.4): CallSessionRecord schema v1, GatewaySessionStore
       protocol, Redis + in-memory backends; moved routes resolve against it:
       /v1/t/{tenant}/calls, TwiML + status webhooks (+ app status-webhook
       relay), WS endpoints, Twilio dial; gateway has no app DB and no
       control-plane client (codelexin initiate routes call the gateway).
       DONE 2026-08-25: vogon/gateway_service/ (session_store.py, routes.py,
       main.py) — uvicorn vogon.gateway_service.main:app --port 8002. The
       driver runs with db_session=None (CallRecord writes guarded); the
       record's per-call app binding selects HttpTurnClient
       (build_turn_client reads config["turn_service"]); app_payload is
       relayed VERBATIM. Smoke: a real text call end-to-end through the
       standalone gateway crossing to /turn/v1 over HTTP
       (test_gateway_service.py).
       REMAINDER DONE 2026-08-25: settings.gateway_url flag (empty =
       monolith path unchanged — production default; set = both initiate
       routes create the call ON the gateway via
       app/services/gateway_client.py, same external contracts). The app
       brings its OWN call_id (gateway CreateCallBody.call_id; duplicate
       = 409) so CallRecord and the session record share one key; the
       gateway dials for twilio mode and returns provider_call_id →
       CallRecord.twilio_call_sid; use_realtime stays monolith (D2).
       New /api/v1/gateway/status receiver maps gateway states onto the
       existing CallRecord vocabulary, binds the CallSid, and never
       regresses a terminal record (auth = parity with the Twilio
       callback it replaces; shared-secret is recorded M2 hardening).
       Pinned by tests/test_initiate_via_gateway.py (5) + the gateway
       call_id pin. Cutover = set GATEWAY_URL (+ GATEWAY_API_KEY) on the
       app and boot the gateway process.
[x] 6. Configuration model (§2.5): GatewayConfig + TenantProfile v1 schema +
       precedence merge into a frozen per-call snapshot stored on the
       session record; the 52 audio keys land in layers 1-2; tenant-scoped
       MetricsSink partitioning {tenant}/{route} (T7: label from
       CallConfig, not TurnContext).
       DONE 2026-08-25 (vogon/gateway_service/config.py): GatewayConfig
       (GATEWAY_* env: api_keys "key:tenant,…", public_base_url,
       session_store, tenant_profiles), TenantProfile v1 (built-in
       defaults from shared settings + per-tenant JSON overrides; typo'd
       profile fails loudly), resolve_call_snapshot frozen onto the
       record at start_call (on_call_config hook). Metrics label:
       {tenant}/{route} in gateway mode, CallConfig.workflow_id in the
       monolith (identical partitioning for codelexin). HONESTY NOTE:
       deep DSP tunables are in the schema but transports still read the
       shared settings (monolith parity) — rewiring those reads is
       recorded follow-through, not done.
[x] 7. Clock normalization at the edge (T1): call-relative event times.
       DONE 2026-08-25 per the user's rulings (order = entry number;
       gateway clock authoritative; turn side rebases): CallLog.ordered()
       flipped to order_seq (no production caller — only its pins moved);
       contract_codec.shift_event_times (pure; 0.0 sentinels preserved;
       durations never shifted); HttpTurnClient shifts −epoch on encode
       (ProcessEventsRequest + EndCallRequest events), http_service
       rebases +epoch on decode (_TurnCall.epoch); anchoring assumption
       (epoch minted just before / on receipt of start_call — skew = one
       request latency, deltas exact) documented in the codec. Pinned by
       tests/test_clock_rebase.py (50,000 s skew end-to-end) and the
       rewritten test_call_event_ordering pins. 339 focused green.
[x] 8. Recording/logging custody (§3): ArtifactStore + CallRecorder
       protocols, artifact routes served by the gateway, log writer moved
       into app/turns, executor LLM_CALLED bypass fixed (T4),
       _recorder._t0 accessor (T8).
       DONE 2026-08-25: vogon/gateway/audio/artifact_store.py (protocol,
       LocalDirStore, S3Store with its own boto3 — app.services import
       gone from the recorder); AudioRecorder takes injected stores +
       CallConfig.recording policy (enabled flag, set by the driver after
       start_call); FOUND+FIXED a real item-4 regression: the driver
       never fired save_and_upload — recordings were silently unsaved
       since 01c5597; now fired at socket close, pinned by a source-shape
       test. T4 via CallLog.append_unretained: envelope-stamped, ordered
       in the ONE per-call sequence, sent through the writer hook, NOT
       retained in entries (honors the 2026-08-02 LLM_CALLED decision);
       raw queue pushes banned by pin. state_version stamped into
       REQUEST_COMPLETED (§2.7 snapshot-equivalence hook). Four
       tenant-checked artifact routes on the gateway service
       (/v1/t/{tenant}/calls/{id}/recording.wav|audio-events.json|
       frames.csv|audio-timeline); LAYOUT AMENDMENT to §3.2: keys stay
       flat by call_id (gateway-minted UUIDs; monolith routes + S3 worker
       compatibility), tenancy enforced at the route via the session
       record. 348 focused green; 9 pins in test_artifact_custody.py.
[x] 9. Barge policy from CallConfig (SAFETY_PHRASES out of gateway
       code); barge judge per D1.
       DONE 2026-08-25: heuristic_verdict/should_swallow_* take
       safety_phrases (gateway default EMPTY — no domain vocabulary
       ships in gateway code; a phrase-less deployment gets the normal
       verdict ladder, pinned); the transport threads
       CallConfig.barge_policy (phrases + semantic_judge gating the LLM
       dispatch); lifecycle supplies the exact former list
       (_SAFETY_BREAKTHROUGH_PHRASES — application-owned now, values
       unchanged). D1: barge_judge.py = gateway-owned Cerebras JSON
       client; app.llm import GONE from the gateway tree.
[x] 10. Import fence: new exact-set ledger — the gateway packages import
       NOTHING from app.* outside the gateway tree; keep the existing
       11-test voice-pillar suite green.
       DONE 2026-08-25: GATEWAY_TREE_ALLOWED exact-set ledger — after
       items 8-9 the whole tree reaches outside for exactly THREE
       targets, each with its reason: app.config (settings; TenantProfile
       migration is the recorded follow-through), app.services.metrics
       (one sink emit), app.textutils (stdlib-only). May shrink, never
       grow.
[x] 11. DEPENDENCIES.md + startup capability checks (AEC/VAD/audioop).
       DONE 2026-08-25: vogon/gateway_service/DEPENDENCIES.md (native
       builds incl. the SpeexDSP system packages and the audioop/py3.13
       pin, service/env table, gateway package set, no-ffmpeg note);
       capabilities.py checks AEC/VAD/audioop at boot —
       GATEWAY_CAPABILITY_POLICY=refuse (default, D5) aborts startup
       with the remedy in the error, warn logs loudly and continues;
       /health reports per-capability status.
[x] 12. Verification: focused seam suite (~204 tests per
       turn-and-gateway-test-plan.md — NOT sims/evals), gateway_dependency_kit
       doubles pointed at the HTTP seam, one end-to-end two-process smoke
       (text mode), then one live Twilio call.
       DONE: focused suite grew to ~360 green; ASGI two-process smoke +
       real-server text smoke (which caught the URL-join and ContextVar
       bugs); PRODUCTION DEPLOY user-verified 2026-08-25 (systemd units,
       nginx /gw, static pair secrets; prod shakedown fixed the shared-
       dotenv extra_forbidden crash and a stale guardrail artifact).
```

### M2 — durability (IN PROGRESS 2026-08-25)

- [x] **TurnContext served from the session store per request** (true
  statelessness; the Phase 0 session-store design is the spec).
  `tests/test_turn_stateless.py` is the acceptance suite. Originally built
  behind `TURN_STATELESS_MODE`; **the flag was REMOVED 2026-08-26 (user
  decision — no rollout flag): stateless is the turn service's only
  serving model.** With it, Redis became the DEFAULT (`redis_enabled=True`,
  code defaults = the local `tools/redis-local.sh` instance at
  `redis://127.0.0.1:6390` standalone; prod env must set `REDIS_URL` +
  `REDIS_CLUSTER_MODE=1` explicitly). Unit tests stay hermetic — the
  pytest conftest forces `redis_enabled=False` so suites use the
  in-process store. The eager-speculation skip is keyed structurally
  (`ctx.request_scoped`, set by rebuild) rather than by config, so the
  legacy in-process driver path keeps its live context and its
  speculation unchanged. What the build actually consisted of:
  - **SessionState v3** (`checkpoint_version=3`, accepts {1,2,3},
    migrate-on-read): the §5.2 tier was NOT enough. A second tier of
    cross-turn TurnContext state existed that reconnect-recovery silently
    lost once per rare reconnect, and a per-request rebuild would have
    lost EVERY turn: `tool_shared` (account data agent prompts read), the
    `events_fired`/`db_written` webhook-dedup sets (loss = duplicate
    webhooks), `notes_buffer`/`booking_result`, the `consecutive_errors`
    and silence/low-confidence ladders, `inflight_playouts` (the
    DeliveryOutcome join — loss = delivery gate broken), analytics rows,
    timestamped transcript lines — now `handler_state`, owned by
    `session_io` — PLUS a bounded call-log tail (60 entries) + clock
    counter, because restore built a fresh EMPTY CallLog and agents'
    `log_slice` would have been blank every turn. Reconnect recovery
    gains the same fidelity as a side effect.
  - **Retry dedup atomic with the commit**: `last_response`
    ({request_id, wire}) rides INSIDE the checkpoint. Phase 0 reserved
    separate `call:{id}:response:{rid}` keys; the in-checkpoint variant
    is strictly better — no second write, no atomicity window, and
    sufficient because per-call requests are serialized so only the LAST
    request can be retried. A retry landing on ANY process finds either
    pre-commit state (reprocess) or committed state carrying its response.
  - **Every request commits** (the trailing commit): fact-only requests,
    silence probes and gate re-asks mutate without reaching the
    executor's commit-then-speak; with the context discarded per request
    those mutations must be committed or lost. A request that RAISES
    commits nothing — the retry reprocesses from the last committed
    state: turn atomicity by construction.
  - **Eager speculation disabled in the mode** (it spans requests; its
    result would die with the discarded context). The overlap-latency
    win is the recorded cost; scratch-persisted eager (Phase 0 §2 turn
    scratch keys) is the recorded follow-up if the cost matters.
    **RESTORED 2026-08-29 by a different mechanism than the recorded
    follow-up:** rather than persisting the speculation, the service PINS
    the context that ends a request with unconsumed eager work
    (`http_service._pin`) and commits the pre-speculation snapshot — the
    cycle anchor — under it. The pin is a per-process cache with the
    checkpoint as the always-correct fallback: crash, reap, stale
    state_version or a request served by another replica all fall back to a
    full turn. Measured through the seam (400 ms agent, 250 ms pause): the
    final costs 151 ms with the eager vs 407 ms without. **Consequence for
    scaling: route by `call_id` when a second turn-service replica is
    added**, or eager and final land on different processes and the reuse
    never fires.
  - **Pending tool tasks drained before each response**
    (`await_pending_tools(drain_all=True)`) — a live task cannot survive
    the discard; `await_before_agent` semantics collapse to end-of-request
    in this mode.
  - **Registry keeps infra only** (`_TurnCall`: db session, rebase epoch,
    lock, response cache, resolved config, ledger writer); `ctx` stays
    None. A process with NO entry adopts the call (`_adopt_call`): the
    ledger writer re-attaches (JSONL append + the atomic stream counter
    continue), the rebase epoch re-derives from the restored context
    (exact in-process; wall-clock-bridged across a restart via the
    `boot_id` check in handler_state).
  - **Config on adoption**: `config_pin.session_token` is now pinned so
    an adopting process re-resolves via `config_recover` — today that
    reaches the same pre-dial primed cache, so adoption across a REAL
    restart resolves only once the control plane serves TenantProfile
    (M2 item below): that item is the last blocker for multi-replica.
  - **Measured cost** (real collections_v3 workflow, 16.8 KB mid-call
    checkpoint): serialize 0.02 ms, wire 2×0.13 ms, rebuild p50 0.48 ms,
    commit ~0.02 ms → **~0.8 ms/request + Redis RTT**. Negligible against
    a 200–500 ms LLM turn.
  - **KILL DRILL PASSED against real Redis** (2026-08-25, local
    source-built redis 8.10.1 — `tools/redis-local.sh`, port 6390,
    standalone mode via `REDIS_CLUSTER_MODE=0`): live Cerebras call,
    `kill -9` on the turn service between turns, fresh interpreter
    adopted the call from Redis and continued it — turn counter resumed
    (turn=2), audio ids continued (resp-3 → resp-4, no aliasing), the
    planner resumed the narrative node from the restored resume stack,
    the checkpoint CAS/TTL Lua and the stream seal ran on a real server,
    and `redis_seq` stayed contiguous 1..60 ACROSS the two writer
    processes (the atomic INCR+XADD proven under an actual process
    death). Checkpoint deleted at end_call; stream + counter sealed at
    24 h together. The prod drill on ElastiCache (cluster mode) remains
    a deploy-day checkbox, no longer a design risk.
  - Accepted residues (recorded): the tiny crash window between the
    executor's mid-turn commit and the trailing commit can double-process
    a retried turn (same acceptance class as commit-then-speak); absolute
    monotonic values inside restored state (ask playback times,
    last_final age) are bridged, not exact, across a true restart —
    their guards compare seconds and the restart gap dwarfs the windows;
    a retried end_call racing a crashed finalize may re-finalize.
- [x] **Retry/backoff + failure policy** (2026-08-26, live-drilled both
  directions):
  - **Gateway → turn-service outage**: `HttpTurnClient` walks a bounded
    backoff ladder (`TURN_RETRY_DELAYS`, default `0.25,1,2,4,8` ≈ 15 s —
    a systemd restart is survivable; the filler hold masks the gap on
    utterance turns). Every replay is safe by construction (per-call
    serialization + the committed response inside the checkpoint).
    Exhaustion raises the TYPED `TurnServiceUnavailable`; the driver then
    speaks the **trouble farewell** (CallConfig.trouble_farewell — domain
    override slot; neutral platform default) and ends with
    `reason="turn_unreachable"`, riding the NORMAL response-execution
    path. 4xx never retries (contract error, not outage). `end_call`
    uses a SHORT ladder (2 attempts) and `_finalize_call` is hardened:
    the gateway's own teardown (call_ended frame, recording, close)
    completes even when end_call cannot land.
  - **Turn-side reaper** (gateway death): a registry call idle past
    `TURN_REAP_IDLE_SEC` (default 900 s) with no in-flight request is
    finalized by the service itself — rebuild, `end_call
    (reason="abandoned")`, call-end tools, log close, keys deleted —
    so a gateway death still produces a COMPLETE call record. Started
    lazily from the handlers (runs in whichever host mounts the router).
    Recorded residue: keys-only orphans (turn-service restart between
    the gateway death and the reap) still clean by the store's 4 h TTL.
  - **abnormal_exit naming fixed**: the read loop's verdict now reaches
    the finalize — a clean client hangup / Twilio carrier stop records
    `reason="hangup"`, a frame timeout `"timeout"`; only genuinely
    unexplained endings remain `abnormal_exit`.
  - Suite: `tests/test_turn_failure_policy.py` (ladder semantics, driver
    farewell + teardown-survives-end_call-failure, hangup naming,
    reaper). Live outage drill 2026-08-26: phase A — turn service
    SIGKILLed mid-call, caller asked into the outage, farewell in 1.6 s
    (fast ladder), `turn_unreachable`, gateway survived; phase B —
    GATEWAY SIGKILLed mid-call, reaper finalized the orphan
    (`CALL_ENDED reason=abandoned`, contiguous ledger, keys deleted).
- [ ] Redis job enqueue / archive worker placement decision.
- [ ] Per-tenant quota enforcement (slot exists in TenantProfile from M1).
- [ ] Control plane serves TenantProfile (Layer 2 source flips from static
  file to fetch — the schema and pre-dial prime are already in place).
  Also unlocks: config re-resolution on call adoption (above) and the
  item-6 DSP tunable reads.
- [x] `app_auth_token` hardening — resolved by the static pair-secret
  decision: the app token travels empty, nothing secret persists on the
  session record.

### M3 — packaging

- [x] Relocate gateway packages into a top-level `voice-gateway/` service dir
  (classifier-service pattern: run.sh, env file, systemd/terraform later);
  Docker images explicitly out of scope until asked. DONE 2026-08-25
  (fed290b) + journald visibility (6c89300).
- [x] Retire monolith mode — the app's own call sockets (`/ws/call`,
  `/ws/twilio`, the `/twiml` fork) deleted, `gateway_url` is an ADDRESS not
  a mode. DONE 2026-08-29 (42751a9).
- [x] Retire the in-process turn client. DONE 2026-08-29: the driver's only
  client is `HttpTurnClient`; `build_turn_client` raises
  `TurnServiceNotConfigured` rather than falling back into the gateway's own
  process, and `AppBinding.base_url` is refused blank at call creation. The
  27 driver tests that scripted the turn side by patching `app.turns.api`
  module attributes now install a client-shaped double
  (`install_turn_client`), and the 21 handler-harness tests drive the REAL
  `/turn/v1` handlers over ASGI (`install_asgi_turn_client`) — the seam that
  serves production. Two consequences worth reading: the eager-speculation
  tests moved to a LIVE context (speculation never starts on a
  request-scoped one, so driving them through the driver would have passed
  while asserting nothing), and a failed opening's finalize is the turn
  side's, not the driver's — see the note in TODO.md about the driver having
  no failure policy for `start_call`.
- Left: widget live verification (standing low-priority item from Phase 0),
  realtime-path decision execution (D2) — the app still serves
  `/ws/twilio-realtime` and lazily imports the gateway tree's
  `realtime_handler` for it.

## 7. What is already done (inherited from Phase 0)

```
[x] Turn API triad as functions; every trigger returns its response
[x] Contract types JSON-representable, kind-tagged events
[x] No shared CallSession; TurnContext/driver split; transports take
    call_id + TransportConfig only (AST-fenced; renamed CallConfig in M1)
[x] transport_analytics as data on EndCallRequest (the template seam)
[x] No cross-clock measurements (metric anchors deleted)
[x] Ordered log appends (Lua INCR+XADD), request_id bracketing
[x] Pre-connect config pin (no in-call control-plane fetch, harness-enforced)
[x] Import ledgers + 204-test focused seam suite; live-verified 2026-08-02
```

## 8. Open decisions (need user input; defaults proposed)

| # | Decision | Options | Proposed default |
|---|---|---|---|
| D1 | Barge judge placement | (a) gateway-owned LLM client, provider+key in gateway config; (b) app-provided verdict endpoint; (c) heuristics only | **(a)** — keeps the tuned latency budget; phrase lists and prompt neutrality come from CallConfig |
| D2 | OpenAI Realtime path | (a) park in monolith; (b) migrate under Turn API | **(a)** for M1 — it bypasses the Turn API entirely and doubles every custody decision |
| D3 | `app_payload` content for codelexin | (a) `{owner_id, workflow_id}` reference, app re-resolves primed config; (b) full config blob | **(a)** — smaller, and the pre-dial prime already exists app-side |
| D4 | Barged-filler discard protocol | (a) speculative response + staged commit + confirm/discard POSTs; (b) LEDGER: turn commits unconditionally, the gateway suppresses the playout locally and reports the physical outcome as a next-request fact; the heard-horizon machinery reconciles | **(b) — DECIDED 2026-08-25 (user).** (a) is a two-phase commit protocol defending a ~2 s window: it needs a staging key, TTL semantics, a resume-time disposition rule and a promote rule. The discard was never a state-correctness need — it is a DELIVERY decision, and "committed but unheard" is already a fully supported production path (every barged playout). Division of knowledge applied: the gateway knows what physically played, the turn service knows what it meant, the delivery ledger is the join. Works identically for the stateful M1 and stateless M2 turn service; a dead call leaves nothing to clean up. |
| D5 | Startup behavior when AEC/VAD can't load | (a) refuse to start; (b) WARN and degrade (today's silent behavior, made loud) | **(a)** for phone deployments, flag-selectable |

## 9. Acceptance

M1 is done when: both services start independently; a text-mode call and a
live Twilio call complete across the HTTP seam; the focused seam suite and
the import ledgers pass; the gateway process imports nothing from `app.*`
outside its own tree, holds no app DB/ORM/control-plane client, and a kill
of the application process mid-call is survived by the gateway long enough
to speak the trouble farewell (basic form; full retry policy is M2).

Additionally, for the session store, config model, and tenancy:
- a gateway restart between dial and answer still serves the TwiML webhook
  and connects the media stream (session store, not process memory);
- every config value the gateway reads is attributable to exactly one of
  the three layers, and the per-call resolved snapshot is on the session
  record;
- a request presenting tenant A's key against tenant B's call gets 404,
  and A's and B's artifacts/metrics land under separate prefixes — covered
  by tests, not inspection.
